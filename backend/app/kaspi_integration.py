from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .kaspi_http_transport import KaspiConfigurationError, KaspiHttpTransport
from .models import MarketplaceAccount, MarketplaceProvider
from .workspace_kaspi import load_workspace_kaspi_connection


@dataclass(frozen=True, slots=True)
class KaspiIntegrationStatus:
    configured: bool
    state: str
    detail: str


def _partner_id() -> str:
    value = os.getenv("KASPI_PARTNER_ID", "").strip()
    if not value:
        raise KaspiConfigurationError("KASPI_PARTNER_ID is not configured")
    return value


def get_kaspi_integration_status(session: Session | None = None) -> KaspiIntegrationStatus:
    if session is not None:
        try:
            connection = load_workspace_kaspi_connection(session)
        except (OperationalError, ProgrammingError):
            session.rollback()
        except Exception as exc:
            return KaspiIntegrationStatus(
                configured=False,
                state="invalid_configuration",
                detail=str(exc),
            )
        else:
            if connection is None:
                return KaspiIntegrationStatus(
                    configured=False,
                    state="not_configured",
                    detail="Kaspi API is not configured for this account",
                )
            return KaspiIntegrationStatus(
                configured=True,
                state="configured",
                detail="Kaspi API is configured for the selected account",
            )

    missing: list[str] = []
    if not os.getenv("KASPI_API_TOKEN", "").strip():
        missing.append("KASPI_API_TOKEN")
    if not os.getenv("KASPI_PARTNER_ID", "").strip():
        missing.append("KASPI_PARTNER_ID")
    if missing:
        return KaspiIntegrationStatus(
            configured=False,
            state="not_configured",
            detail=f"{', '.join(missing)} is not configured",
        )

    try:
        KaspiHttpTransport.from_environment().close()
        _partner_id()
    except KaspiConfigurationError as exc:
        return KaspiIntegrationStatus(
            configured=False,
            state="invalid_configuration",
            detail=str(exc),
        )

    return KaspiIntegrationStatus(
        configured=True,
        state="configured",
        detail="Kaspi order transport and marketplace account identity are configured",
    )


def build_kaspi_order_transport(session: Session | None = None) -> KaspiHttpTransport:
    """Build a fail-closed transport for the selected workspace.

    The environment-only path remains for command-line compatibility. Runtime
    API code always supplies a session and therefore uses encrypted per-account
    credentials.
    """
    if session is not None:
        try:
            connection = load_workspace_kaspi_connection(session)
        except (OperationalError, ProgrammingError):
            session.rollback()
            connection = None
        if connection is None:
            raise KaspiConfigurationError(
                "Kaspi API is not configured for the selected account"
            )
        return connection.transport()
    _partner_id()
    return KaspiHttpTransport.from_environment()


def ensure_kaspi_marketplace_account(session: Session) -> MarketplaceAccount:
    """Return the selected workspace's Kaspi account.

    The environment fallback keeps legacy CLI/tests functional before the
    credential bootstrap migration has run.
    """
    try:
        connection = load_workspace_kaspi_connection(session)
    except (OperationalError, ProgrammingError):
        session.rollback()
        connection = None
    if connection is not None:
        account = session.get(MarketplaceAccount, connection.account_id)
        if account is None:
            raise KaspiConfigurationError("Kaspi marketplace account is missing")
        return account

    partner_id = _partner_id()
    account = session.scalar(
        select(MarketplaceAccount).where(
            MarketplaceAccount.provider == MarketplaceProvider.KASPI.value,
            MarketplaceAccount.external_account_id == partner_id,
        )
    )
    if account is not None:
        return account

    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id=partner_id,
        display_name=os.getenv("KASPI_SHOP_NAME", "Kaspi Shop").strip() or "Kaspi Shop",
        timezone=os.getenv("KASPI_TIMEZONE", "Asia/Almaty").strip() or "Asia/Almaty",
    )
    session.add(account)
    session.flush()
    return account
