from __future__ import annotations

from typing import Any, Protocol

from .mapper import map_seller_order_facts
from .models import SellerOrderFacts


class KaspiSellerTransport(Protocol):
    def fetch_order_details(self, *, merchant_id: str, order_code: str) -> dict[str, Any]: ...


class KaspiSellerClient:
    """Application-facing adapter over an authenticated Seller transport."""

    def __init__(self, transport: KaspiSellerTransport) -> None:
        self._transport = transport

    def fetch_order_facts(self, *, merchant_id: str, order_code: str) -> SellerOrderFacts:
        payload = self._transport.fetch_order_details(
            merchant_id=merchant_id,
            order_code=order_code,
        )
        facts = map_seller_order_facts(payload)
        if facts.order_code not in (None, order_code):
            raise ValueError(
                f"Kaspi Seller returned order {facts.order_code!r} for requested {order_code!r}"
            )
        return facts
