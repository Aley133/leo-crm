from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .decision_engine import CommerceOrderStage, OrderDecisionEngine, OrderDecisionFacts


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

    @property
    def is_preorder_requested(self) -> bool:
        return self.purchase_request_id is not None and self.purchase_status in {"draft", "requested"}

    @property
    def is_procurement_in_transit(self) -> bool:
        return self.purchase_request_id is not None and self.purchase_status in {"ordered", "partially_received"}


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
    snapshot_stage: str | None = None
    snapshot_state: str | None = None
    snapshot_status: str | None = None
    snapshot_observed_at: datetime | None = None
    snapshot_assembled: bool | None = None
    snapshot_transmitted_to_courier: bool | None = None
    snapshot_arrived_at_pickup: bool | None = None
    snapshot_returned_to_warehouse: bool | None = None

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def unresolved_lines(self) -> int:
        return sum(1 for line in self.lines if not line.is_resolved)

    @property
    def procurement_required_lines(self) -> int:
        if self.stage not in {CommerceOrderStage.NEW, CommerceOrderStage.ACCEPTED, CommerceOrderStage.PREORDER}:
            return 0
        return sum(1 for line in self.lines if line.procurement_state == ProcurementState.REQUIRED)

    def effective_procurement_state(self, line: CommerceOrderLine) -> ProcurementState:
        if line.procurement_state != ProcurementState.REQUIRED:
            return line.procurement_state
        if self.stage not in {CommerceOrderStage.NEW, CommerceOrderStage.ACCEPTED, CommerceOrderStage.PREORDER}:
            return ProcurementState.NOT_REQUIRED
        return ProcurementState.REQUIRED

    @property
    def has_preorder_request(self) -> bool:
        return any(line.is_preorder_requested for line in self.lines)

    @property
    def has_procurement_in_transit(self) -> bool:
        return any(line.is_procurement_in_transit for line in self.lines)

    @property
    def has_procurement_in_progress(self) -> bool:
        return any(line.procurement_state == ProcurementState.IN_PROGRESS for line in self.lines)

    @property
    def all_procurement_received(self) -> bool:
        return bool(self.lines) and all(line.procurement_state == ProcurementState.RECEIVED for line in self.lines)

    @property
    def recognized_revenue(self) -> Decimal:
        if self.stage in {CommerceOrderStage.CANCELLED, CommerceOrderStage.RETURNED}:
            return Decimal("0")
        return self.total_amount

    @property
    def decision_facts(self) -> OrderDecisionFacts:
        return OrderDecisionFacts(
            marketplace_status=self.status or self.original_status,
            snapshot_stage=self.snapshot_stage,
            snapshot_state=self.snapshot_state,
            snapshot_status=self.snapshot_status,
            assembled=self.snapshot_assembled,
            transmitted_to_courier=self.snapshot_transmitted_to_courier,
            arrived_at_pickup=self.snapshot_arrived_at_pickup,
            returned_to_warehouse=self.snapshot_returned_to_warehouse,
            has_lines=bool(self.lines),
            all_procurement_received=self.all_procurement_received,
            has_procurement_in_progress=self.has_procurement_in_progress,
        )

    @property
    def stage_source(self) -> str:
        return OrderDecisionEngine.source(self.decision_facts)

    @property
    def stage(self) -> CommerceOrderStage:
        return OrderDecisionEngine.decide(self.decision_facts)


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
