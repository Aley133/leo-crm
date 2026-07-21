from __future__ import annotations

from typing import Any

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
from .marketplace_import import normalize_kaspi_order
from .marketplace_sync import sync_kaspi_order_page
from .models import MarketplaceImportCheckpoint


router = APIRouter(
    prefix="/api/marketplaces/kaspi",
    tags=["kaspi"],
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


def _diagnostic_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key != "entries"
    }


def _diagnostic_order(payload: dict[str, Any]) -> dict[str, Any]:
    attributes = _diagnostic_attributes(payload)
    normalized = normalize_kaspi_order(payload)
    raw_attributes = payload.get("attributes")
    entries = raw_attributes.get("entries", []) if isinstance(raw_attributes, dict) else []
    date_like_fields = {
        key: value
        for key, value in attributes.items()
        if any(
            marker in key.lower()
            for marker in (
                "date",
                "time",
                "arrival",
                "shipment",
                "delivery",
                "reservation",
                "courier",
                "completion",
            )
        )
    }
    return {
        "id": payload.get("id"),
        "code": attributes.get("code") or attributes.get("orderCode"),
        "source_status": attributes.get("status") or attributes.get("orderStatus"),
        "source_state": attributes.get("state") or attributes.get("fulfillmentState"),
        "attribute_keys": sorted(attributes.keys()),
        "date_like_fields": date_like_fields,
        "source_attributes": attributes,
        "source_relationships": payload.get("relationships") or {},
        "entries": entries if isinstance(entries, list) else [],
        "entries_count": len(entries) if isinstance(entries, list) else 0,
        "leo_normalized_status": normalized.status,
        "leo_original_status": normalized.original_status,
        "source_payload": payload,
    }


@router.get("/orders/diagnostics/raw-page")
def inspect_raw_order_page(
    page_number: int = Query(default=1, ge=1, le=1000),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Inspect one live Kaspi page without importing or advancing checkpoints."""
    try:
        transport, marketplace_account_id = _bootstrap_live_import()
    except KaspiConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        page = transport.fetch_orders(
            cursor=str(page_number),
            updated_after=None,
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
        "page_number": page_number,
        "fetched_count": len(page.items),
        "next_cursor": page.next_cursor,
        "watermark_at": page.watermark_at,
        "items": [
            _diagnostic_order(payload)
            for payload in page.items
        ],
    }


@router.get("/orders/{order_code}/diagnostics/raw")
def inspect_raw_order_by_code(
    order_code: str,
    page_size: int = Query(default=50, ge=1, le=100),
    max_pages: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Find one live Kaspi order by seller-visible code without importing it.

    This endpoint is deliberately read-only. It does not use or update the
    marketplace checkpoint. Pages are scanned only inside the configured live
    Kaspi lookback window so the result reflects the payload available to the
    production Orders API.
    """
    normalized_code = order_code.strip()
    if not normalized_code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="order_code must not be empty",
        )

    try:
        transport, marketplace_account_id = _bootstrap_live_import()
    except KaspiConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    pages_scanned = 0
    cursor: str | None = "1"
    try:
        while cursor is not None and pages_scanned < max_pages:
            page = transport.fetch_orders(
                cursor=cursor,
                updated_after=None,
                limit=page_size,
            )
            pages_scanned += 1
            for payload in page.items:
                attributes = _diagnostic_attributes(payload)
                payload_code = str(
                    attributes.get("code")
                    or attributes.get("orderCode")
                    or ""
                ).strip()
                if payload_code == normalized_code:
                    return {
                        "marketplace_account_id": marketplace_account_id,
                        "pages_scanned": pages_scanned,
                        "found": True,
                        "item": _diagnostic_order(payload),
                    }
            cursor = page.next_cursor
    except KaspiTransportError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    finally:
        transport.close()

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "message": "Kaspi order was not found in the live API lookback window",
            "order_code": normalized_code,
            "pages_scanned": pages_scanned,
            "page_size": page_size,
        },
    )


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
    page_size: int = Query(default=10, ge=1, le=100),
    max_pages: int = Query(default=3, ge=1, le=20),
    max_duration_seconds: int = Query(default=25, ge=5, le=60),
) -> dict[str, str | int | bool | None]:
    """Resume from checkpoint with page and request-time safety bounds.

    The endpoint may intentionally return ``completed=false``. Invoke it again
    to resume from the persisted checkpoint until ``completed=true``.
    """
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
            max_duration_seconds=max_duration_seconds,
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
        "stopped_reason": result.stopped_reason,
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
