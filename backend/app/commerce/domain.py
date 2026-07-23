from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .profit_calculator import calculate_line_economics, kaspi_logistics_per_unit


class CommerceOrderStage(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PREORDER = "preorder"
    ASSEMBLY = "assembly"
    HANDOVER = "handover"
    SHIPPING = "shipping"
    CANCELLING = "cancelling"
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
    procurement_unit_cost: Decimal | None = None
    procurement_source_name: str | None = None
    inventory_allocated_quantity: int = 0

    @property
    def is_resolved(self) -> bool:
        return self.product_id is not None

    @property
    def is_fully_allocated_from_inventory(self) -> bool:
        return self.quantity > 0 and self.inventory_allocated_quantity >= self.quantity

    @property
    def procurement_state(self) -> ProcurementState:
        if self.is_fully_allocated_from_inventory:
            return ProcurementState.NOT_REQUIRED
        if self.purchase_request_id is None:
            return ProcurementState.REQUIRED
        if self.purchase_status in {"received", "closed"}:
            return ProcurementState.RECEIVED
        if self.purchase_status == "cancelled":
            return ProcurementState.CANCELLED
        return ProcurementState.IN_PROGRESS

    @property
    def procurement_total_cost(self) -> Decimal | None:
        if self.procurement_unit_cost is None:
            return None
        return self.procurement_unit_cost * self.quantity

    @property
    def gross_margin(self) -> Decimal | None:
        total_cost = self.procurement_total_cost
        if total_cost is None:
            return None
        return self.line_total - total_cost

    @property
    def gross_margin_pct(self) -> Decimal | None:
        if self.line_total <= 0 or self.gross_margin is None:
            return None
        return (self.gross_margin / self.line_total * Decimal("100")).quantize(Decimal("0.01"))

    @property
    def kaspi_commission(self) -> Decimal:
        return self._fees.kaspi_commission

    @property
    def tax(self) -> Decimal:
        return self._fees.tax

    @property
    def logistics(self) -> Decimal:
        return kaspi_logistics_per_unit(self.unit_price) * self.quantity

    @property
    def net_profit(self) -> Decimal | None:
        return None if self.procurement_unit_cost is None else self._economics.net_profit

    @property
    def net_margin_pct(self) -> Decimal | None:
        return None if self.procurement_unit_cost is None else self._economics.net_margin_pct

    @property
    def _fees(self):
        return calculate_line_economics(
            unit_sale_price=self.unit_price,
            quantity=self.quantity,
            procurement_unit_cost=Decimal("0"),
        )

    @property
    def _economics(self):
        if self.procurement_unit_cost is None:
            raise RuntimeError("procurement cost is required for order economics")
        return calculate_line_economics(
            unit_sale_price=self.unit_price,
            quantity=self.quantity,
            procurement_unit_cost=self.procurement_unit_cost,
        )


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
        if self.stage in {
            CommerceOrderStage.CANCELLING,
            CommerceOrderStage.CANCELLED,
            CommerceOrderStage.RETURNED,
        }:
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
