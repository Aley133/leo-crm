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
    IN_TRANSIT = "in_transit"
    ASSEMBLY = "assembly"
    HANDOVER = "handover"
    SHIPPING = "shipping"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"
    UNKNOWN = "unknown"


SNAPSHOT_STAGE_MAP: dict[str, CommerceOrderStage] = {
    "NEW": CommerceOrderStage.NEW,
    "ACCEPTED": CommerceOrderStage.ACCEPTED,
    "ACCEPTED_BY_MERCHANT": CommerceOrderStage.ACCEPTED,
    "PREORDER": CommerceOrderStage.PREORDER,
    "PRE_ORDER": CommerceOrderStage.PREORDER,
    "IN_TRANSIT": CommerceOrderStage.IN_TRANSIT,
    "ASSEMBLY": CommerceOrderStage.ASSEMBLY,
    "PACKING": CommerceOrderStage.ASSEMBLY,
    "PACKAGING": CommerceOrderStage.ASSEMBLY,
    "HANDOVER": CommerceOrderStage.HANDOVER,
    "READY_FOR_HANDOVER": CommerceOrderStage.HANDOVER,
    "TRANSFER": CommerceOrderStage.HANDOVER,
    "SHIPPING": CommerceOrderStage.SHIPPING,
    "DELIVERY": CommerceOrderStage.SHIPPING,
    "KASPI_DELIVERY": CommerceOrderStage.SHIPPING,
    "DELIVERED": CommerceOrderStage.DELIVERED,
    "CANCELLED": CommerceOrderStage.CANCELLED,
    "CANCELED": CommerceOrderStage.CANCELLED,
    "RETURNED": CommerceOrderStage.RETURNED,
}


NO_NEW_PROCUREMENT_STAGES = {
    CommerceOrderStage.ASSEMBLY,
    CommerceOrderStage.HANDOVER,
    CommerceOrderStage.SHIPPING,
    CommerceOrderStage.DELIVERED,
    CommerceOrderStage.CANCELLED,
    CommerceOrderStage.RETURNED,
}


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
    snapshot_observed_at: datetime | None = None

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def unresolved_lines(self) -> int:
        return sum(1 for line in self.lines if not line.is_resolved)

    def effective_procurement_state(self, line: CommerceOrderLine) -> ProcurementState:
        """Return the state that must be shown in Orders Center.

        A line without a purchase request is only actionable while the order can
        still wait for procurement. Once Kaspi has moved the order to packing,
        handover, shipping or a terminal stage, showing "required" is misleading.
        Existing purchase facts remain visible as received/in progress/cancelled.
        """

        state = line.procurement_state
        if state == ProcurementState.REQUIRED and self.stage in NO_NEW_PROCUREMENT_STAGES:
            return ProcurementState.NOT_REQUIRED
        return state

    @property
    def procurement_required_lines(self) -> int:
        return sum(
            1
            for line in self.lines
            if self.effective_procurement_state(line) == ProcurementState.REQUIRED
        )

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
    def stage_source(self) -> str:
        if self.snapshot_stage and self.snapshot_stage.strip().upper() in SNAPSHOT_STAGE_MAP:
            return "snapshot"
        return "marketplace_order"

    @property
    def stage(self) -> CommerceOrderStage:
        if self.snapshot_stage:
            snapshot_stage = SNAPSHOT_STAGE_MAP.get(self.snapshot_stage.strip().upper())
            if snapshot_stage is not None:
                return snapshot_stage

        normalized_status = self.status.strip().lower()
        if normalized_status in {"cancelled", "canceled"}:
            return CommerceOrderStage.CANCELLED
        if normalized_status == "returned":
            return CommerceOrderStage.RETURNED
        if normalized_status == "delivered":
            return CommerceOrderStage.DELIVERED
        if normalized_status in {"shipping", "delivery", "kaspi_delivery"}:
            return CommerceOrderStage.SHIPPING
        if normalized_status in {"handover", "ready_for_handover", "transfer"}:
            return CommerceOrderStage.HANDOVER
        if normalized_status in {"assembly", "packing", "packaging"}:
            return CommerceOrderStage.ASSEMBLY
        if normalized_status == "new":
            return CommerceOrderStage.NEW
        if normalized_status in {"preorder", "pre_order"}:
            return CommerceOrderStage.PREORDER
        if normalized_status in {"accepted", "accepted_by_merchant"}:
            if self.all_procurement_received:
                return CommerceOrderStage.ASSEMBLY
            return CommerceOrderStage.PREORDER
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
