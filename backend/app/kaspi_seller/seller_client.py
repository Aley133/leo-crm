from __future__ import annotations

from typing import Any, Protocol

from .mapper import map_seller_order_facts, map_seller_order_snapshot
from .models import SellerOrderFacts, SellerOrderSnapshot


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
        self._validate_order_code(actual=facts.order_code, requested=order_code)
        return facts

    def fetch_order_snapshot(
        self,
        *,
        merchant_id: str,
        order_code: str,
    ) -> SellerOrderSnapshot:
        payload = self._transport.fetch_order_details(
            merchant_id=merchant_id,
            order_code=order_code,
        )
        snapshot = map_seller_order_snapshot(payload, merchant_id=merchant_id)
        self._validate_order_code(actual=snapshot.order_code, requested=order_code)
        return snapshot

    @staticmethod
    def _validate_order_code(*, actual: str | None, requested: str) -> None:
        if actual not in (None, requested):
            raise ValueError(
                f"Kaspi Seller returned order {actual!r} for requested {requested!r}"
            )
