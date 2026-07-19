from __future__ import annotations

import os
from dataclasses import dataclass

from .kaspi_http_transport import KaspiConfigurationError, KaspiHttpTransport


@dataclass(frozen=True, slots=True)
class KaspiIntegrationStatus:
    configured: bool
    state: str
    detail: str


def get_kaspi_integration_status() -> KaspiIntegrationStatus:
    token_present = bool(os.getenv("KASPI_API_TOKEN", "").strip())
    if not token_present:
        return KaspiIntegrationStatus(
            configured=False,
            state="not_configured",
            detail="KASPI_API_TOKEN is not configured",
        )

    try:
        KaspiHttpTransport.from_environment().close()
    except KaspiConfigurationError as exc:
        return KaspiIntegrationStatus(
            configured=False,
            state="invalid_configuration",
            detail=str(exc),
        )

    return KaspiIntegrationStatus(
        configured=True,
        state="configured",
        detail="Kaspi order transport is configured",
    )


def build_kaspi_order_transport() -> KaspiHttpTransport:
    """Build a fail-closed Kaspi transport from deployment environment values."""

    return KaspiHttpTransport.from_environment()
