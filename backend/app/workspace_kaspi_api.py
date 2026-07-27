from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .kaspi_credentials_crypto import (
    KaspiCredentialConfigurationError,
    encrypt_api_token,
)
from .kaspi_credentials_models import KaspiAccountCredential
from .models import MarketplaceAccount, MarketplaceProvider
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/kaspi", tags=["workspace-kaspi"])


class KaspiConnectionRequest(BaseModel):
    shop_name: str = Field(min_length=1, max_length=255)
    partner_id: str = Field(min_length=1, max_length=128)
    api_token: str = Field(min_length=1, max_length=4096)
    timezone: str = Field(default="Asia/Almaty", min_length=1, max_length=64)


class KaspiConnectionResponse(BaseModel):
    configured: bool
    marketplace_account_id: int | None = None
    shop_name: str | None = None
    partner_id: str | None = None
    token_masked: str | None = None
    timezone: str | None = None


def _mask_token(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


@router.get("", response_model=KaspiConnectionResponse)
def get_connection(
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> KaspiConnectionResponse:
    credential = db.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == principal.workspace_id
        )
    )
    if credential is None:
        return KaspiConnectionResponse(configured=False)

    account = db.get(MarketplaceAccount, credential.marketplace_account_id)
    if account is None or account.workspace_id != principal.workspace_id:
        raise HTTPException(status_code=409, detail="Kaspi account ownership is inconsistent")

    return KaspiConnectionResponse(
        configured=True,
        marketplace_account_id=account.id,
        shop_name=account.display_name,
        partner_id=credential.partner_id,
        token_masked="configured",
        timezone=account.timezone,
    )


@router.put("", response_model=KaspiConnectionResponse)
def save_connection(
    payload: KaspiConnectionRequest,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> KaspiConnectionResponse:
    partner_id = payload.partner_id.strip()
    shop_name = payload.shop_name.strip()
    timezone = payload.timezone.strip()
    try:
        encrypted_token = encrypt_api_token(payload.api_token)
    except KaspiCredentialConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    credential = db.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == principal.workspace_id
        )
    )
    account: MarketplaceAccount | None = None
    if credential is not None:
        account = db.get(MarketplaceAccount, credential.marketplace_account_id)
        if account is None or account.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=409, detail="Kaspi account ownership is inconsistent")

    if account is None:
        account = MarketplaceAccount(
            workspace_id=principal.workspace_id,
            provider=MarketplaceProvider.KASPI.value,
            external_account_id=partner_id,
            display_name=shop_name,
            timezone=timezone,
        )
        db.add(account)
        db.flush()
    else:
        account.external_account_id = partner_id
        account.display_name = shop_name
        account.timezone = timezone

    if credential is None:
        credential = KaspiAccountCredential(
            workspace_id=principal.workspace_id,
            marketplace_account_id=account.id,
            partner_id=partner_id,
            api_token_encrypted=encrypted_token,
        )
        db.add(credential)
    else:
        credential.partner_id = partner_id
        credential.api_token_encrypted = encrypted_token

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kaspi account already exists in this workspace",
        ) from exc

    return KaspiConnectionResponse(
        configured=True,
        marketplace_account_id=account.id,
        shop_name=account.display_name,
        partner_id=credential.partner_id,
        token_masked=_mask_token(payload.api_token),
        timezone=account.timezone,
    )
