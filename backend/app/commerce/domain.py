from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .profit_calculator import (
    allocate_order_logistics,
    calculate_line_economics,
    kaspi_logistics_per_unit,
)


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
    production_completed_quantity: int = 0
    incoming_reserved_quantity: int = 0
    order_logistics_share: Decimal | None = None
    image_url: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.product_id is not None

    @property
    def is_fully_allocated_from_inventory(self) -> bool:
        return self.quantity > 0 and self.inventory_allocated_quantity >= self.quantity

    @property
    def uncovered_quantity(self) -> int:
        covered = self.inventory_allocated_quantity + self.incoming_reserved_quantity
        return max(int(self.quantity) - int(covered), 0)

    @property
    def procurement_state(self) -> ProcurementState:
        # "Изготовлено" is a stronger signal than an older purchase workflow:
        # the product is physically ready for this order even if a stale
        # purchase request still exists.
        if (
            self.production_completed_quantity > 0
            and self.is_fully_allocated_from_inventory
        ):
            return ProcurementState.NOT_REQUIRED
        # An explicit purchase request is authoritative for preorder readiness.
        if self.purchase_request_id is not None:
            if self.purchase_status in {"received", "closed"}:
                return ProcurementState.RECEIVED
            if self.purchase_status == "cancelled":
                return ProcurementState.CANCELLED
            return ProcurementState.IN_PROGRESS
        if self.is_fully_allocated_from_inventory:
            return ProcurementState.NOT_REQUIRED
        if self.incoming_reserved_quantity > 0 and self.uncovered_quantity == 0:
            return ProcurementState.IN_PROGRESS
        return ProcurementState.REQUIRED

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
        if self.order_logistics_share is not None:
            return self.order_logistics_share
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
            logistics_cost=self.order_logistics_share,
        )

    @property
    def _economics(self):
        if self.procurement_unit_cost is None:
            raise RuntimeError("procurement cost is required for order economics")
        return calculate_line_economics(
            unit_sale_price=self.unit_price,
            quantity=self.quantity,
            procurement_unit_cost=self.procurement_unit_cost,
            logistics_cost=self.order_logistics_share,
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
    manual_stage: str | None = None
    manual_stage_reason: str | None = None
    manual_stage_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        logistics_shares = allocate_order_logistics(
            order_total=self.total_amount,
            line_totals=tuple(line.line_total for line in self.lines),
        )
        object.__setattr__(
            self,
            "lines",
            tuple(
                replace(line, order_logistics_share=share)
                for line, share in zip(self.lines, logistics_shares, strict=True)
            ),
        )

    @property
    def stage(self) -> CommerceOrderStage:
        try:
            source_stage = CommerceOrderStage(self.status)
        except ValueError:
            source_stage = CommerceOrderStage.UNKNOWN

        # Kaspi is authoritative for regular seller orders and every stage after
        # the warehouse. A real preorder may advance to packaging only when
        # every ordered unit has a physical FIFO allocation. Incoming batches
        # are deliberately excluded until they are marked as received.
        if source_stage in {
            CommerceOrderStage.HANDOVER,
            CommerceOrderStage.SHIPPING,
            CommerceOrderStage.CANCELLING,
            CommerceOrderStage.DELIVERED,
            CommerceOrderStage.CANCELLED,
            CommerceOrderStage.RETURNED,
        }:
            return source_stage

        if self.manual_stage:
            try:
                return CommerceOrderStage(self.manual_stage)
            except ValueError:
                pass

        if source_stage in {
            CommerceOrderStage.ACCEPTED,
            CommerceOrderStage.PREORDER,
        }:
            return (
                CommerceOrderStage.ASSEMBLY
                if self._ready_for_packaging
                else CommerceOrderStage.PREORDER
            )
        return source_stage

    @property
    def _ready_for_packaging(self) -> bool:
        if not self.lines:
            return False
        return all(
            line.product_id is not None
            and line.inventory_allocated_quantity >= line.quantity
            for line in self.lines
        )

    @property
    def stage_source(self) -> str:
        if self.manual_stage:
            return "manual_owner_correction"
        if self.status in {
            CommerceOrderStage.ACCEPTED.value,
            CommerceOrderStage.PREORDER.value,
        } and self._ready_for_packaging:
            return "kaspi_orders_api+received_fifo"
        return "kaspi_orders_api"

    @property
    def logistics(self) -> Decimal:
        return sum((line.logistics for line in self.lines), Decimal("0"))

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
        return sum(1 for line in self.lines if line.uncovered_quantity > 0)

    @property
    def procurement_required_units(self) -> int:
        if self.stage not in {CommerceOrderStage.NEW, CommerceOrderStage.PREORDER}:
            return 0
        return sum(line.uncovered_quantity for line in self.lines)

    @property
    def incoming_reserved_units(self) -> int:
        return sum(line.incoming_reserved_quantity for line in self.lines)

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

    @property
    def confirmed_net_profit(self) -> Decimal:
        if self.stage in {
            CommerceOrderStage.CANCELLING,
            CommerceOrderStage.CANCELLED,
            CommerceOrderStage.RETURNED,
        }:
            return Decimal("0")
        return sum((line.net_profit for line in self.lines if line.net_profit is not None), Decimal("0"))

    @property
    def confirmed_profit_units(self) -> int:
        if self.stage in {
            CommerceOrderStage.CANCELLING,
            CommerceOrderStage.CANCELLED,
            CommerceOrderStage.RETURNED,
        }:
            return 0
        return sum(line.quantity for line in self.lines if line.net_profit is not None)


@dataclass(frozen=True, slots=True)
class CommerceSummary:
    orders_count: int
    units_count: int
    revenue: Decimal
    confirmed_net_profit: Decimal
    confirmed_profit_units: int
    active_orders: int
    delivered_orders: int
    cancelled_orders: int
    unresolved_lines: int
    procurement_required_lines: int
    procurement_required_units: int
    incoming_reserved_units: int
