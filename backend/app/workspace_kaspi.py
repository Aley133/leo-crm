from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .kaspi_credentials_crypto import decrypt_api_token, encrypt_api_token
from .kaspi_http_transport import KaspiHttpSettings, KaspiHttpTransport
from .models import MarketplaceAccount, MarketplaceProvider
from .workspace_context import LEGACY_WORKSPACE_ID, current_workspace_id, workspace_context
from .workspace_models import KaspiAccountCredential, Workspace


@dataclass(frozen=True, slots=True)
class WorkspaceKaspiConnection:
    workspace_id: int
    account_id: int
    partner_id: str
    shop_name: str
    timezone: str
    api_token: str

    def transport(self, *, lookback_days: int | None = None) -> KaspiHttpTransport:
        environment = KaspiHttpSettings.from_environment_defaults(
            self.api_token,
            initial_lookback_days=lookback_days,
        )
        return KaspiHttpTransport(environment)


def load_workspace_kaspi_connection(
    db: Session,
    *,
    workspace_id: int | None = None,
) -> WorkspaceKaspiConnection | None:
    selected = current_workspace_id() if workspace_id is None else int(workspace_id)
    credential = db.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == selected
        )
    )
    if credential is None:
        return None
    account = db.get(MarketplaceAccount, credential.marketplace_account_id)
    if account is None or account.workspace_id != selected:
        raise RuntimeError("Kaspi account ownership is inconsistent")
    return WorkspaceKaspiConnection(
        workspace_id=selected,
        account_id=account.id,
        partner_id=credential.partner_id,
        shop_name=account.display_name,
        timezone=account.timezone,
        api_token=decrypt_api_token(credential.api_token_encrypted),
    )


def list_workspace_kaspi_connections(db: Session) -> list[WorkspaceKaspiConnection]:
    """Load every active configured Kaspi account for background workers."""
    db.info["include_all_workspaces"] = True
    try:
        workspace_ids = list(
            db.scalars(
                select(Workspace.id)
                .where(Workspace.is_active.is_(True))
                .order_by(Workspace.id)
            ).all()
        )
        result: list[WorkspaceKaspiConnection] = []
        for workspace_id in workspace_ids:
            connection = load_workspace_kaspi_connection(
                db,
                workspace_id=int(workspace_id),
            )
            if connection is not None:
                result.append(connection)
        return result
    finally:
        db.info.pop("include_all_workspaces", None)


def bootstrap_legacy_workspace_connection(db: Session) -> bool:
    """Copy deployment credentials into workspace 1 once.

    Existing stored credentials always win, so changing account 1 in the CRM is
    never undone by a later Render restart.
    """

    token = os.getenv("KASPI_API_TOKEN", "").strip()
    partner_id = os.getenv("KASPI_PARTNER_ID", "").strip()
    if not token or not partner_id:
        return False

    with workspace_context(LEGACY_WORKSPACE_ID):
        workspace = db.get(Workspace, LEGACY_WORKSPACE_ID)
        if workspace is None:
            workspace = Workspace(
                id=LEGACY_WORKSPACE_ID,
                name="BARWORK",
                slug="barwork",
                is_active=True,
            )
            db.add(workspace)
            db.flush()
        credential = db.scalar(
            select(KaspiAccountCredential).where(
                KaspiAccountCredential.workspace_id == LEGACY_WORKSPACE_ID
            )
        )
        if credential is not None:
            return False
        account = db.scalar(
            select(MarketplaceAccount).where(
                MarketplaceAccount.provider == MarketplaceProvider.KASPI.value,
                MarketplaceAccount.external_account_id == partner_id,
            )
        )
        if account is None:
            account = MarketplaceAccount(
                workspace_id=LEGACY_WORKSPACE_ID,
                provider=MarketplaceProvider.KASPI.value,
                external_account_id=partner_id,
                display_name=os.getenv("KASPI_SHOP_NAME", "Kaspi Shop").strip()
                or "Kaspi Shop",
                timezone=os.getenv("KASPI_TIMEZONE", "Asia/Almaty").strip()
                or "Asia/Almaty",
            )
            db.add(account)
            db.flush()
        db.add(
            KaspiAccountCredential(
                workspace_id=LEGACY_WORKSPACE_ID,
                marketplace_account_id=account.id,
                partner_id=partner_id,
                api_token_encrypted=encrypt_api_token(token),
            )
        )
        return True


def validate_kaspi_connection(api_token: str) -> None:
    transport = KaspiHttpTransport(
        KaspiHttpSettings.from_environment_defaults(api_token, initial_lookback_days=1)
    )
    try:
        transport.fetch_orders(cursor="1", updated_after=None, limit=1)
    finally:
        transport.close()
