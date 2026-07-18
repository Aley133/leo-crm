from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from backend.app.monitoring import offer_fingerprint


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    supplier_product_id: int
    url: str
    external_id: str

    def __post_init__(self) -> None:
        if self.supplier_product_id < 1:
            raise ValueError("supplier_product_id must be positive")
        if not self.url.strip():
            raise ValueError("url must not be empty")
        if not self.external_id.strip():
            raise ValueError("external_id must not be empty")


@dataclass(frozen=True, slots=True)
class NormalizedOffer:
    supplier_product_id: int
    price: Decimal | None
    old_price: Decimal | None
    available: bool | None
    stock: int | None
    delivery_days: int | None
    seller: str | None
    adapter_schema_version: str
    observed_at: datetime
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.supplier_product_id < 1:
            raise ValueError("supplier_product_id must be positive")
        if self.price is not None and self.price < 0:
            raise ValueError("price must not be negative")
        if self.old_price is not None and self.old_price < 0:
            raise ValueError("old_price must not be negative")
        if self.stock is not None and self.stock < 0:
            raise ValueError("stock must not be negative")
        if self.delivery_days is not None and self.delivery_days < 0:
            raise ValueError("delivery_days must not be negative")
        if not self.adapter_schema_version.strip():
            raise ValueError("adapter_schema_version must not be empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    @property
    def fingerprint(self) -> str:
        return offer_fingerprint(
            supplier_product_id=self.supplier_product_id,
            price=self.price,
            available=self.available,
            stock=self.stock,
            delivery_days=self.delivery_days,
            seller=self.seller,
            adapter_schema_version=self.adapter_schema_version,
        )


class SupplierAdapter(Protocol):
    code: str
    access_strategy: str

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        """Fetch one supplier card and return normalized business facts."""
