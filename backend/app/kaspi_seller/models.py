from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SellerOrderStep:
    step: str
    actual_time: str | None = None
    planned_time: str | None = None
    timeout_time: str | None = None
    range_from: str | None = None
    range_to: str | None = None
    typename: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SellerOrderStep":
        return cls(
            step=str(payload.get("step") or "").strip().upper(),
            actual_time=_optional_text(payload.get("actualTime")),
            planned_time=_optional_text(payload.get("plannedTime")),
            timeout_time=_optional_text(payload.get("timeoutTime")),
            range_from=_optional_text(payload.get("from")),
            range_to=_optional_text(payload.get("to")),
            typename=_optional_text(payload.get("__typename")),
        )


@dataclass(frozen=True, slots=True)
class SellerOrderMarker:
    name: str
    creation_time: str | None = None


@dataclass(frozen=True, slots=True)
class SellerOrderLine:
    entry_id: int | str | None
    merchant_sku: str | None
    product_code: str | None
    barcode: str | None
    title: str | None
    quantity: int
    total_price: int | None


@dataclass(frozen=True, slots=True)
class SellerOrderDelivery:
    mode: str | None
    assembled: bool
    transmitted_to_courier: bool
    order_arrived: bool
    returned_to_warehouse: bool
    transmission_planned_at: str | None
    planned_delivery_at: str | None
    planned_point_delivery_at: str | None
    assembled_at: str | None
    actual_delivery_at: str | None


@dataclass(frozen=True, slots=True)
class SellerOrderWarehouse:
    name: str | None
    city_id: str | None
    city_name: str | None
    pickup_type: str | None


@dataclass(frozen=True, slots=True)
class SellerOrderFacts:
    order_code: str | None
    state: str
    status: str
    preorder: bool
    is_order_arrived: bool
    kd_assembled: bool
    kd_transmitted_to_courier: bool
    steps: tuple[SellerOrderStep, ...]
    marker_names: tuple[str, ...]

    def step_actual_time(self, step_name: str) -> str | None:
        wanted = step_name.strip().upper()
        for step in self.steps:
            if step.step == wanted:
                return step.actual_time
        return None


@dataclass(frozen=True, slots=True)
class SellerOrderSnapshot:
    """Normalized Seller Cabinet order state consumed by the rest of LEO.

    Raw GraphQL names are contained at the mapper boundary. The snapshot keeps
    all order lines and both SimpleOrderStep and RangeOrderStep data so later
    CRM features do not need to read Kaspi response envelopes directly.
    """

    merchant_id: str | None
    order_code: str
    state: str
    status: str
    stage: str | None
    preorder: bool
    creation_time: str | None
    modification_time: str | None
    customer_name: str | None
    recipient_name: str | None
    delivery: SellerOrderDelivery
    warehouse: SellerOrderWarehouse | None
    lines: tuple[SellerOrderLine, ...]
    steps: tuple[SellerOrderStep, ...]
    markers: tuple[SellerOrderMarker, ...]
    schema_version: str | None = None

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def total_price(self) -> int | None:
        prices = [line.total_price for line in self.lines if line.total_price is not None]
        return sum(prices) if prices else None

    @property
    def marker_names(self) -> tuple[str, ...]:
        return tuple(marker.name for marker in self.markers)


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
