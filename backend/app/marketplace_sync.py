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
    evidence, normalized orders, events, execution result and checkpoint are
    committed atomically. A failed persistence transaction cannot advance the
    checkpoint.
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
