from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .marketplace_import import import_kaspi_order
from .marketplace_transport import MarketplaceOrderTransport
from .models import (
    MarketplaceImportCheckpoint,
    MarketplaceImportExecution,
    MarketplaceImportStatus,
    MarketplaceOrder,
    OutboxEvent,
)


SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class MarketplaceSyncResult:
    execution_id: UUID
    fetched_count: int
    imported_count: int
    updated_count: int
    next_cursor: str | None


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
    evidence, normalized orders, events, outbox records, execution result and
    checkpoint are committed atomically. A failed persistence transaction cannot
    advance the checkpoint or publish a downstream business event.
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

        imported_count = 0
        updated_count = 0
        with session_factory() as session:
            with session.begin():
                execution = session.get(MarketplaceImportExecution, execution_id)
                if execution is None:
                    raise RuntimeError("Marketplace import execution disappeared")

                for payload in page.items:
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

                    if result.created or result.changed:
                        order = session.get(MarketplaceOrder, result.order_id)
                        if order is None:
                            raise RuntimeError("Imported marketplace order disappeared")
                        event_type = (
                            "marketplace.order.created"
                            if result.created
                            else "marketplace.order.updated"
                        )
                        idempotency_key = (
                            f"{event_type}:{order.id}:v{order.version}"
                        )
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
            fetched_count=len(page.items),
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
