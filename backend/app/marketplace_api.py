from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from .auth import require_service_token
from .db import SessionLocal
from .kaspi_http_transport import KaspiConfigurationError, KaspiTransportError
from .kaspi_integration import (
    build_kaspi_order_transport,
    ensure_kaspi_marketplace_account,
    get_kaspi_integration_status,
)
from .marketplace_full_sync import sync_kaspi_orders
from .marketplace_sync import sync_kaspi_order_page
from .models import MarketplaceImportCheckpoint


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


def _bootstrap_live_import():
    transport = build_kaspi_order_transport()
    with SessionLocal() as session:
        with session.begin():
            account = ensure_kaspi_marketplace_account(session)
            marketplace_account_id = account.id
    return transport, marketplace_account_id


@router.post("/orders/sync-page")
def sync_order_page(
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, str | int | None]:
    """Import one bounded live Kaspi page using deployment configuration."""
    try:
        transport, marketplace_account_id = _bootstrap_live_import()
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


@router.post("/orders/full-sync")
def full_sync_orders(
    page_size: int = Query(default=50, ge=1, le=100),
    max_pages: int = Query(default=10, ge=1, le=100),
) -> dict[str, str | int | bool | None]:
    """Resume from checkpoint and process several pages with a safety cap."""
    try:
        transport, marketplace_account_id = _bootstrap_live_import()
    except KaspiConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        result = sync_kaspi_orders(
            SessionLocal,
            transport,
            marketplace_account_id=marketplace_account_id,
            page_size=page_size,
            max_pages=max_pages,
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
        "pages_processed": result.pages_processed,
        "fetched_count": result.fetched_count,
        "imported_count": result.imported_count,
        "updated_count": result.updated_count,
        "next_cursor": result.next_cursor,
        "completed": result.completed,
    }


@router.post("/orders/checkpoint/reset")
def reset_order_checkpoint(
    confirm: bool = Query(default=False),
) -> dict[str, int | bool]:
    """Rewind only the orders checkpoint; existing orders remain idempotently stored."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirm=true to reset the Kaspi orders checkpoint",
        )

    try:
        _, marketplace_account_id = _bootstrap_live_import()
    except KaspiConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    with SessionLocal() as session:
        with session.begin():
            checkpoint = session.scalar(
                select(MarketplaceImportCheckpoint).where(
                    MarketplaceImportCheckpoint.marketplace_account_id
                    == marketplace_account_id,
                    MarketplaceImportCheckpoint.stream_name == "orders",
                )
            )
            reset = checkpoint is not None
            if checkpoint is not None:
                session.delete(checkpoint)

    return {
        "marketplace_account_id": marketplace_account_id,
        "reset": reset,
    }
