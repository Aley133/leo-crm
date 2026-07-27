from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, get_db
from .kaspi_credentials_crypto import (
    KaspiCredentialConfigurationError,
    KaspiCredentialDecryptError,
    decrypt_api_token,
)
from .kaspi_credentials_models import KaspiAccountCredential
from .kaspi_http_transport import KaspiHttpSettings, KaspiHttpTransport
from .marketplace_sync import sync_kaspi_order_page
from .models import MarketplaceAccount
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/kaspi/import", tags=["workspace-kaspi-import"])
JOBS: dict[str, dict[str, Any]] = {}


def _load_owned_credentials(
    db: Session,
    *,
    workspace_id: int,
) -> tuple[KaspiAccountCredential, MarketplaceAccount, str]:
    credential = db.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == workspace_id
        )
    )
    if credential is None:
        raise HTTPException(status_code=409, detail="Kaspi account is not configured")

    account = db.get(MarketplaceAccount, credential.marketplace_account_id)
    if account is None or account.workspace_id != workspace_id:
        raise HTTPException(status_code=409, detail="Kaspi account ownership is inconsistent")

    try:
        api_token = decrypt_api_token(credential.api_token_encrypted)
    except (KaspiCredentialConfigurationError, KaspiCredentialDecryptError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return credential, account, api_token


def _run_sync(job_id: str, *, marketplace_account_id: int, api_token: str, days: int) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(UTC).isoformat()
    transport = KaspiHttpTransport(
        KaspiHttpSettings(
            api_token=api_token,
            initial_lookback_days=days,
        )
    )
    try:
        result = sync_kaspi_order_page(
            SessionLocal,
            transport,
            marketplace_account_id=marketplace_account_id,
            stream_name="workspace_orders",
            limit=100,
        )
        job.update(
            {
                "status": "completed",
                "finished_at": datetime.now(UTC).isoformat(),
                "execution_id": str(result.execution_id),
                "fetched_count": result.fetched_count,
                "imported_count": result.imported_count,
                "updated_count": result.updated_count,
                "next_cursor": result.next_cursor,
                "message": "Kaspi orders imported",
            }
        )
    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "finished_at": datetime.now(UTC).isoformat(),
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        transport.close()


async def _run_sync_async(
    job_id: str,
    *,
    marketplace_account_id: int,
    api_token: str,
    days: int,
) -> None:
    await asyncio.to_thread(
        _run_sync,
        job_id,
        marketplace_account_id=marketplace_account_id,
        api_token=api_token,
        days=days,
    )


@router.post("")
def start_import(
    background_tasks: BackgroundTasks,
    days: int = Query(default=1, ge=1, le=31),
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> dict:
    _credential, account, api_token = _load_owned_credentials(
        db,
        workspace_id=principal.workspace_id,
    )
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "job_id": job_id,
        "workspace_id": principal.workspace_id,
        "marketplace_account_id": account.id,
        "days": days,
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "fetched_count": 0,
        "imported_count": 0,
        "updated_count": 0,
        "message": "Kaspi import queued",
    }
    background_tasks.add_task(
        _run_sync_async,
        job_id,
        marketplace_account_id=account.id,
        api_token=api_token,
        days=days,
    )
    return dict(JOBS[job_id])


@router.get("/{job_id}")
def get_import(
    job_id: str,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
) -> dict:
    job = JOBS.get(job_id)
    if job is None or job.get("workspace_id") != principal.workspace_id:
        raise HTTPException(status_code=404, detail="Import job not found")
    return dict(job)
