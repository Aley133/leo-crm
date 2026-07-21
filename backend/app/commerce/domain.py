from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ProcurementState(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    IN_PROGRESS = "in_progress"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class CommerceOrderStage(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PREORDER = "preorder"
    ASSEMBLY = "assembly"
    HANDOVER = "handover"
    SHIPPING = "shipping"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommerceOrderLine:
    line_id: int
    product_id: int | None
    external_product_id: str | None
    merchant_sku: str | None
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    purchase_request_id: str | None
    purchase_status: str | None

    @property
    def is_resolved(self) -> bool:
        return self.product_id is not None

    @property
    def procurement_state(self) -> ProcurementState:
        if self.purchase_request_id is None:
            return ProcurementState.REQUIRED
        if self.purchase_status in {"received", "closed"}:
            return ProcurementState.RECEIVED
        if self.purchase_status == "cancelled":
            return ProcurementState.CANCELLED
        return ProcurementState.IN_PROGRESS


@dataclass(frozen=True, slots=True)
class CommerceOrder:
    order_id: int
    external_code: str | None
    marketplace: str
    status: str
    currency: str
    total_amount: Decimal
    ordered_at: datetime | None
    delivered_at: datetime | None
    lines: tuple[CommerceOrderLine, ...]
    original_status: str = "UNKNOWN"

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def unresolved_lines(self) -> int:
        return sum(1 for line in self.lines if not line.is_resolved)

    @property
    def procurement_required_lines(self) -> int:
        if self.status in {"cancelled", "returned", "delivered", "shipping"}:
            return 0
        return sum(
            1
            for line in self.lines
            if line.procurement_state == ProcurementState.REQUIRED
        )

    @property
    def has_procurement_in_progress(self) -> bool:
        return any(
            line.procurement_state == ProcurementState.IN_PROGRESS
            for line in self.lines
        )

    @property
    def all_procurement_received(self) -> bool:
        return bool(self.lines) and all(
            line.procurement_state == ProcurementState.RECEIVED
            for line in self.lines
        )

    @property
    def stage(self) -> CommerceOrderStage:
        if self.status == "cancelled":
            return CommerceOrderStage.CANCELLED
        if self.status == "returned":
            return CommerceOrderStage.RETURNED
        if self.status == "delivered":
            return CommerceOrderStage.DELIVERED
        if self.status == "shipping":
            return CommerceOrderStage.SHIPPING
        if self.status == "assembly":
            return CommerceOrderStage.ASSEMBLY
        if self.status == "new":
            return CommerceOrderStage.NEW
        if self.status == "accepted":
            if self.has_procurement_in_progress:
                return CommerceOrderStage.PREORDER
            if self.all_procurement_received:
                return CommerceOrderStage.ASSEMBLY
            # Until Warehouse/Reservations exists, an accepted Kaspi order cannot
            # be truthfully classified as either preorder or assembly. Keep the
            # operational stage explicit instead of guessing from a missing PR.
            return CommerceOrderStage.ACCEPTED
        return CommerceOrderStage.UNKNOWN


@dataclass(frozen=True, slots=True)
class CommerceSummary:
    orders_count: int
    units_count: int
    revenue: Decimal
    active_orders: int
    delivered_orders: int
    cancelled_orders: int
    unresolved_lines: int
    procurement_required_lines: int
