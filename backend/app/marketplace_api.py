from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import require_service_token
from .db import SessionLocal
from .kaspi_http_transport import KaspiConfigurationError, KaspiTransportError
from .kaspi_integration import (
    build_kaspi_order_transport,
    ensure_kaspi_marketplace_account,
    get_kaspi_integration_status,
)
from .marketplace_sync import sync_kaspi_order_page


router = APIRouter(
    prefix="/api/marketplaces/kaspi",
    tags=["marketplaces", "kaspi"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/status")
def kaspi_status() -> dict[str, str | bool]:
    integration = get_kaspi_integration_status()
    return {
        "configured": integration.configured,
        "state": integration.state,
        "detail": integration.detail,
    }


@router.post("/orders/sync-page")
def sync_order_page(
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, str | int | None]:
    """Import one bounded live Kaspi page using deployment configuration."""
    try:
        transport = build_kaspi_order_transport()
        with SessionLocal() as session:
            with session.begin():
                account = ensure_kaspi_marketplace_account(session)
                marketplace_account_id = account.id
    except KaspiConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        result = sync_kaspi_order_page(
            SessionLocal,
            transport,
            marketplace_account_id=marketplace_account_id,
            limit=limit,
        )
    except KaspiTransportError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    finally:
        transport.close()

    return {
        "marketplace_account_id": marketplace_account_id,
        "execution_id": str(result.execution_id),
        "fetched_count": result.fetched_count,
        "imported_count": result.imported_count,
        "updated_count": result.updated_count,
        "next_cursor": result.next_cursor,
    }
