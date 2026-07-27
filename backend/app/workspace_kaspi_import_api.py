from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .kaspi_credentials_crypto import (
    KaspiCredentialConfigurationError,
    KaspiCredentialDecryptError,
    decrypt_api_token,
)
from .kaspi_credentials_models import KaspiAccountCredential
from .kaspi_raw_receiver_jobs import create_job, public_job, run_job
from .models import MarketplaceAccount
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/kaspi/import", tags=["workspace-kaspi-import"])


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
    job_id = create_job(
        days=days,
        timezone_name=account.timezone,
        workspace_id=principal.workspace_id,
        marketplace_account_id=account.id,
        api_token=api_token,
    )
    background_tasks.add_task(run_job, job_id)
    return {
        "job_id": job_id,
        "status": "queued",
        "workspace_id": principal.workspace_id,
        "marketplace_account_id": account.id,
    }


@router.get("/{job_id}")
def get_import(
    job_id: str,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
) -> dict:
    job = public_job(job_id)
    if job is None or job.get("workspace_id") != principal.workspace_id:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job
