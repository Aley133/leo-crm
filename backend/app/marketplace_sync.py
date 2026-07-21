from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .kaspi_order_payload import canonicalize_kaspi_order_payload
from .marketplace_import import import_kaspi_order
from .marketplace_transport import MarketplaceOrderTransport
from .models import (
    MarketplaceImportCheckpoint,
    MarketplaceImportExecution,
    MarketplaceImportStatus,
    MarketplaceOrder,
    OutboxEvent,
)
from .product_identity_service import ensure_marketplace_listing_for_order_line


SessionFactory = Callable[[], Session]
_KASPI_DETAIL_WORKERS = 6


@dataclass(frozen=True, slots=True)
class MarketplaceSyncResult:
    execution_id: UUID
    fetched_count: int
    imported_count: int
    updated_count: int
    next_cursor: str | None


def _seller_order_code(payload: dict[str, Any]) -> str | None:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get("code") or attributes.get("orderCode")
    normalized = str(value or "").strip()
    return normalized or None


def _requires_seller_detail(payload: dict[str, Any]) -> bool:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return False
    return str(attributes.get("status") or "").strip().upper() == "ACCEPTED_BY_MERCHANT"


def _hydrate_seller_truth(
    transport: MarketplaceOrderTransport,
    items: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Replace coarse accepted list payloads with seller-detail payloads.

    Kaspi's page response often keeps every active delivery order under
    ``ACCEPTED_BY_MERCHANT`` and omits the seller-operation facts required to
    distinguish preorder, packaging, handover and actual courier transmission.
    The optional direct lookup already used by diagnostics exposes those facts.
    Detail failures are isolated per order so one transient Kaspi response never
    aborts the whole committed page.
    """

    fetch_by_code = getattr(transport, "fetch_order_by_code", None)
    if not callable(fetch_by_code):
        return items

    result = list(items)
    candidates: dict[Any, int] = {}
    with ThreadPoolExecutor(max_workers=_KASPI_DETAIL_WORKERS) as executor:
        for index, payload in enumerate(items):
            if not _requires_seller_detail(payload):
                continue
            code = _seller_order_code(payload)
            if code is None:
                continue
            candidates[executor.submit(fetch_by_code, code)] = index

        for future in as_completed(candidates):
            index = candidates[future]
            try:
                detailed = future.result()
            except Exception:
                continue
            if isinstance(detailed, dict):
                result[index] = detailed

    return tuple(result)


def sync_kaspi_order_page(
    session_factory: SessionFactory,
    transport: MarketplaceOrderTransport,
    *,
    marketplace_account_id: int,
    stream_name: str = "orders",
    limit: int = 100,
) -> MarketplaceSyncResult:
    """Fetch one bounded page, then persist it in one business transaction.

    The external request happens with no SQLAlchemy session open. The page's raw
    evidence, normalized orders, listing identities or resolution issues, events,
    outbox records, execution result and checkpoint are committed atomically. A
    failed persistence transaction cannot advance the checkpoint or publish a
    downstream business event.
    """

    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    with session_factory() as session:
        checkpoint = session.scalar(
            select(MarketplaceImportCheckpoint).where(
                MarketplaceImportCheckpoint.marketplace_account_id == marketplace_account_id,
                MarketplaceImportCheckpoint.stream_name == stream_name,
            )
        )
        cursor = checkpoint.cursor if checkpoint is not None else None
        watermark_at = checkpoint.watermark_at if checkpoint is not None else None

    with session_factory() as session:
        with session.begin():
            execution = MarketplaceImportExecution(
                marketplace_account_id=marketplace_account_id,
                status=MarketplaceImportStatus.RUNNING.value,
            )
            session.add(execution)
            session.flush()
            execution_id = execution.id

    try:
        page = transport.fetch_orders(
            cursor=cursor,
            updated_after=watermark_at,
            limit=limit,
        )
        source_items = _hydrate_seller_truth(transport, page.items)

        imported_count = 0
        updated_count = 0
        with session_factory() as session:
            with session.begin():
                execution = session.get(MarketplaceImportExecution, execution_id)
                if execution is None:
                    raise RuntimeError("Marketplace import execution disappeared")

                for source_payload in source_items:
                    payload = canonicalize_kaspi_order_payload(source_payload)
                    result = import_kaspi_order(
                        session,
                        marketplace_account_id=marketplace_account_id,
                        payload=payload,
                        import_execution_id=execution_id,
                        checkpoint_stream=stream_name,
                    )
                    if result.created:
                        imported_count += 1
                    elif result.changed:
                        updated_count += 1

                    order = session.get(MarketplaceOrder, result.order_id)
                    if order is None:
                        raise RuntimeError("Imported marketplace order disappeared")

                    # Identity discovery is part of the same caller-owned import
                    # transaction. Running it for unchanged orders also backfills
                    # listings for orders imported before Product Identity Sprint 1.
                    for order_line in order.lines:
                        ensure_marketplace_listing_for_order_line(
                            session,
                            marketplace_account_id=marketplace_account_id,
                            order_line=order_line,
                        )

                    if result.created or result.changed:
                        event_type = (
                            "marketplace.order.created"
                            if result.created
                            else "marketplace.order.updated"
                        )
                        idempotency_key = f"{event_type}:{order.id}:v{order.version}"
                        existing_outbox = session.scalar(
                            select(OutboxEvent).where(
                                OutboxEvent.idempotency_key == idempotency_key
                            )
                        )
                        if existing_outbox is None:
                            session.add(
                                OutboxEvent(
                                    aggregate_type="marketplace_order",
                                    aggregate_id=str(order.id),
                                    event_type=event_type,
                                    idempotency_key=idempotency_key,
                                    payload_json={
                                        "order_id": order.id,
                                        "marketplace_account_id": order.marketplace_account_id,
                                        "external_order_id": order.external_order_id,
                                        "status": order.status,
                                        "original_status": order.original_status,
                                        "version": order.version,
                                    },
                                )
                            )

                checkpoint = session.scalar(
                    select(MarketplaceImportCheckpoint)
                    .where(
                        MarketplaceImportCheckpoint.marketplace_account_id
                        == marketplace_account_id,
                        MarketplaceImportCheckpoint.stream_name == stream_name,
                    )
                    .with_for_update()
                )
                if checkpoint is None:
                    checkpoint = MarketplaceImportCheckpoint(
                        marketplace_account_id=marketplace_account_id,
                        stream_name=stream_name,
                    )
                    session.add(checkpoint)
                checkpoint.cursor = page.next_cursor
                if page.watermark_at is not None:
                    checkpoint.watermark_at = page.watermark_at

                execution.status = MarketplaceImportStatus.SUCCEEDED.value
                execution.finished_at = datetime.now(UTC)
                execution.imported_count = imported_count
                execution.updated_count = updated_count
                execution.failed_count = 0

        return MarketplaceSyncResult(
            execution_id=execution_id,
            fetched_count=len(source_items),
            imported_count=imported_count,
            updated_count=updated_count,
            next_cursor=page.next_cursor,
        )
    except Exception as exc:
        with session_factory() as session:
            with session.begin():
                execution = session.get(MarketplaceImportExecution, execution_id)
                if execution is not None:
                    execution.status = MarketplaceImportStatus.FAILED.value
                    execution.finished_at = datetime.now(UTC)
                    execution.failed_count = 1
                    execution.error_summary = str(exc)[:2000]
        raise
