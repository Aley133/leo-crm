from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .kaspi_credentials_crypto import (
    KaspiCredentialConfigurationError,
    KaspiCredentialDecryptError,
    decrypt_api_token,
    encrypt_api_token,
)
from .kaspi_http_transport import KaspiTransportError
from .models import MarketplaceAccount, MarketplaceProvider
from .workspace_context import current_workspace_id
from .workspace_kaspi import validate_kaspi_connection
from .workspace_models import KaspiAccountCredential, Workspace


router = APIRouter(
    prefix="/api/workspaces",
    tags=["workspaces"],
    dependencies=[Depends(require_service_token)],
)


class WorkspaceConnectionWrite(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    partner_id: str = Field(min_length=1, max_length=128)
    api_token: str = Field(min_length=1, max_length=4096)
    timezone: str = Field(default="Asia/Almaty", min_length=1, max_length=64)


class WorkspaceConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    partner_id: str | None = Field(default=None, min_length=1, max_length=128)
    api_token: str | None = Field(default=None, min_length=1, max_length=4096)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceRead(BaseModel):
    id: int
    name: str
    slug: str
    active: bool
    configured: bool
    marketplace_account_id: int | None
    partner_id: str | None
    timezone: str | None
    feed_url: str


def _read_workspace(
    workspace: Workspace,
    credential: KaspiAccountCredential | None,
    account: MarketplaceAccount | None,
) -> WorkspaceRead:
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        active=workspace.is_active,
        configured=credential is not None and account is not None,
        marketplace_account_id=None if account is None else account.id,
        partner_id=None if credential is None else credential.partner_id,
        timezone=None if account is None else account.timezone,
        feed_url=f"/feeds/kaspi/{workspace.slug}/catalog.xml",
    )


def _workspace_rows(db: Session) -> list[WorkspaceRead]:
    db.info["include_all_workspaces"] = True
    try:
        workspaces = list(
            db.scalars(
                select(Workspace)
                .where(Workspace.is_active.is_(True))
                .order_by(Workspace.id)
            ).all()
        )
        credentials = {
            item.workspace_id: item
            for item in db.scalars(select(KaspiAccountCredential)).all()
        }
        account_ids = [item.marketplace_account_id for item in credentials.values()]
        accounts = {
            item.id: item
            for item in db.scalars(
                select(MarketplaceAccount).where(MarketplaceAccount.id.in_(account_ids))
            ).all()
        } if account_ids else {}
        return [
            _read_workspace(
                workspace,
                credentials.get(workspace.id),
                accounts.get(credentials[workspace.id].marketplace_account_id)
                if workspace.id in credentials
                else None,
            )
            for workspace in workspaces
        ]
    finally:
        db.info.pop("include_all_workspaces", None)


def _validate_token(api_token: str, *, enabled: bool) -> None:
    if not enabled:
        return
    try:
        validate_kaspi_connection(api_token)
    except KaspiTransportError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Kaspi API connection failed: {exc}",
        ) from exc


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)) -> list[WorkspaceRead]:
    return _workspace_rows(db)


@router.get("/current", response_model=WorkspaceRead)
def read_current_workspace(db: Session = Depends(get_db)) -> WorkspaceRead:
    selected = current_workspace_id()
    rows = {item.id: item for item in _workspace_rows(db)}
    if selected not in rows:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return rows[selected]


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceConnectionWrite,
    validate: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> WorkspaceRead:
    _validate_token(payload.api_token, enabled=validate)
    try:
        encrypted_token = encrypt_api_token(payload.api_token)
    except (KaspiCredentialConfigurationError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    db.info["include_all_workspaces"] = True
    try:
        count = int(db.scalar(select(func.count()).select_from(Workspace)) or 0)
        if count >= 20:
            raise HTTPException(status_code=409, detail="Workspace limit reached")
        name = payload.name.strip()
        workspace = Workspace(
            name=name,
            slug=f"kaspi-{secrets.token_hex(5)}",
            is_active=True,
        )
        db.add(workspace)
        db.flush()
        account = MarketplaceAccount(
            workspace_id=workspace.id,
            provider=MarketplaceProvider.KASPI.value,
            external_account_id=payload.partner_id.strip(),
            display_name=name,
            timezone=payload.timezone.strip(),
        )
        db.add(account)
        db.flush()
        credential = KaspiAccountCredential(
            workspace_id=workspace.id,
            marketplace_account_id=account.id,
            partner_id=payload.partner_id.strip(),
            api_token_encrypted=encrypted_token,
        )
        db.add(credential)
        db.commit()
        return _read_workspace(workspace, credential, account)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Kaspi partner ID is already connected to this account",
        ) from exc
    finally:
        db.info.pop("include_all_workspaces", None)


@router.put("/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: int,
    payload: WorkspaceConnectionUpdate,
    validate: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> WorkspaceRead:
    db.info["include_all_workspaces"] = True
    try:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None or not workspace.is_active:
            raise HTTPException(status_code=404, detail="Workspace not found")
        credential = db.scalar(
            select(KaspiAccountCredential).where(
                KaspiAccountCredential.workspace_id == workspace_id
            )
        )
        account = (
            None
            if credential is None
            else db.get(MarketplaceAccount, credential.marketplace_account_id)
        )
        if credential is None or account is None:
            raise HTTPException(status_code=409, detail="Kaspi account is not configured")

        raw_token: str | None = None
        if payload.api_token is not None:
            raw_token = payload.api_token.strip()
            _validate_token(raw_token, enabled=validate)
            try:
                credential.api_token_encrypted = encrypt_api_token(raw_token)
            except KaspiCredentialConfigurationError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        elif validate:
            try:
                raw_token = decrypt_api_token(credential.api_token_encrypted)
            except (KaspiCredentialConfigurationError, KaspiCredentialDecryptError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            _validate_token(raw_token, enabled=True)

        if payload.name is not None:
            workspace.name = payload.name.strip()
            account.display_name = workspace.name
        if payload.partner_id is not None:
            partner_id = payload.partner_id.strip()
            credential.partner_id = partner_id
            account.external_account_id = partner_id
        if payload.timezone is not None:
            account.timezone = payload.timezone.strip()
        db.commit()
        return _read_workspace(workspace, credential, account)
    except HTTPException:
        db.rollback()
        raise
    except (IntegrityError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        db.info.pop("include_all_workspaces", None)


@router.post("/{workspace_id}/test")
def test_workspace_connection(
    workspace_id: int,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    db.info["include_all_workspaces"] = True
    try:
        credential = db.scalar(
            select(KaspiAccountCredential).where(
                KaspiAccountCredential.workspace_id == workspace_id
            )
        )
        if credential is None:
            raise HTTPException(status_code=404, detail="Kaspi account is not configured")
        try:
            token = decrypt_api_token(credential.api_token_encrypted)
            validate_kaspi_connection(token)
        except (KaspiCredentialConfigurationError, KaspiCredentialDecryptError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except KaspiTransportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"workspace_id": workspace_id, "connected": True}
    finally:
        db.info.pop("include_all_workspaces", None)
