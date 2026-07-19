from __future__ import annotations

from dataclasses import dataclass
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


def sync_kaspi_orders(
    session_factory: SessionFactory,
    transport: MarketplaceOrderTransport,
    *,
    marketplace_account_id: int,
    page_size: int = 50,
    max_pages: int = 20,
) -> MarketplaceFullSyncResult:
    """Process multiple committed pages with an explicit safety bound.

    Every page keeps the atomicity guarantees of ``sync_kaspi_order_page``.
    A failed page is not counted and its checkpoint is not advanced. The caller
    can safely invoke this function again to resume from the last committed page.
    """

    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")
    if max_pages < 1 or max_pages > 100:
        raise ValueError("max_pages must be between 1 and 100")

    pages_processed = 0
    fetched_count = 0
    imported_count = 0
    updated_count = 0
    next_cursor: str | None = None

    while pages_processed < max_pages:
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
    )
