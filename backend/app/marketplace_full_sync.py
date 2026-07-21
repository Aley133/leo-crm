from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable

from sqlalchemy.orm import Session

from .marketplace_sync import MarketplaceSyncResult, sync_kaspi_order_page
from .marketplace_transport import MarketplaceOrderTransport


SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class MarketplaceFullSyncResult:
    pages_processed: int
    fetched_count: int
    imported_count: int
    updated_count: int
    next_cursor: str | None
    completed: bool
    stopped_reason: str | None = None


def sync_kaspi_orders(
    session_factory: SessionFactory,
    transport: MarketplaceOrderTransport,
    *,
    marketplace_account_id: int,
    page_size: int = 10,
    max_pages: int = 3,
    max_duration_seconds: int = 25,
) -> MarketplaceFullSyncResult:
    """Process committed pages with page and wall-clock safety bounds.

    Every page keeps the atomicity guarantees of ``sync_kaspi_order_page``.
    A failed page is not counted and its checkpoint is not advanced. The caller
    can safely invoke this function again to resume from the last committed page.

    The time budget is checked between pages. A single marketplace request may
    still take longer than the budget, but the endpoint will never start another
    page after the budget has been exhausted.
    """

    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")
    if max_pages < 1 or max_pages > 100:
        raise ValueError("max_pages must be between 1 and 100")
    if max_duration_seconds < 5 or max_duration_seconds > 120:
        raise ValueError("max_duration_seconds must be between 5 and 120")

    pages_processed = 0
    fetched_count = 0
    imported_count = 0
    updated_count = 0
    next_cursor: str | None = None
    started_at = monotonic()

    while pages_processed < max_pages:
        if pages_processed > 0 and monotonic() - started_at >= max_duration_seconds:
            return MarketplaceFullSyncResult(
                pages_processed=pages_processed,
                fetched_count=fetched_count,
                imported_count=imported_count,
                updated_count=updated_count,
                next_cursor=next_cursor,
                completed=False,
                stopped_reason="time_budget_exhausted",
            )

        page: MarketplaceSyncResult = sync_kaspi_order_page(
            session_factory,
            transport,
            marketplace_account_id=marketplace_account_id,
            limit=page_size,
        )
        pages_processed += 1
        fetched_count += page.fetched_count
        imported_count += page.imported_count
        updated_count += page.updated_count
        next_cursor = page.next_cursor

        if next_cursor is None or page.fetched_count == 0:
            return MarketplaceFullSyncResult(
                pages_processed=pages_processed,
                fetched_count=fetched_count,
                imported_count=imported_count,
                updated_count=updated_count,
                next_cursor=next_cursor,
                completed=True,
            )

    return MarketplaceFullSyncResult(
        pages_processed=pages_processed,
        fetched_count=fetched_count,
        imported_count=imported_count,
        updated_count=updated_count,
        next_cursor=next_cursor,
        completed=False,
        stopped_reason="page_limit_reached",
    )
