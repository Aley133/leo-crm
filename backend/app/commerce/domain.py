from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


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


class ProcurementState(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    IN_PROGRESS = "in_progress"
    RECEIVED = "received"
    CANCELLED = "cancelled"


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
    purchase_version: int | None = None

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
    marketplace_account_id: int | None = None
    marketplace_external_account_id: str | None = None

    @property
    def stage(self) -> CommerceOrderStage:
        # In the raw-receiver pipeline `accepted` is emitted only for
        # preOrder=true. Regular accepted stock orders are normalized to assembly.
        if self.status == CommerceOrderStage.ACCEPTED.value:
            return CommerceOrderStage.PREORDER
        try:
            return CommerceOrderStage(self.status)
        except ValueError:
            return CommerceOrderStage.UNKNOWN

    @property
    def stage_source(self) -> str:
        return "kaspi_orders_api"

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def unresolved_lines(self) -> int:
        return sum(1 for line in self.lines if not line.is_resolved)

    @property
    def procurement_required_lines(self) -> int:
        if self.stage not in {CommerceOrderStage.NEW, CommerceOrderStage.PREORDER}:
            return 0
        return sum(1 for line in self.lines if line.procurement_state == ProcurementState.REQUIRED)

    def effective_procurement_state(self, line: CommerceOrderLine) -> ProcurementState:
        if line.procurement_state != ProcurementState.REQUIRED:
            return line.procurement_state
        if self.stage not in {CommerceOrderStage.NEW, CommerceOrderStage.PREORDER}:
            return ProcurementState.NOT_REQUIRED
        return ProcurementState.REQUIRED

    @property
    def recognized_revenue(self) -> Decimal:
        if self.stage in {CommerceOrderStage.CANCELLED, CommerceOrderStage.RETURNED}:
            return Decimal("0")
        return self.total_amount


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
