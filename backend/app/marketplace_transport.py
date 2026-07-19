from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class MarketplaceOrderPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    watermark_at: datetime | None = None


class MarketplaceOrderTransport(Protocol):
    """Fetch marketplace order pages without owning a database transaction."""

    def fetch_orders(
        self,
        *,
        cursor: str | None,
        updated_after: datetime | None,
        limit: int,
    ) -> MarketplaceOrderPage:
        ...
