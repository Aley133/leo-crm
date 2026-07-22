from datetime import UTC, datetime
from decimal import Decimal

from backend.app.commerce.domain import (
    CommerceOrder,
    CommerceOrderLine,
    CommerceOrderStage,
    ProcurementState,
)
from backend.app.commerce.service import CommerceService


def _line(*, product_id=1, purchase_request_id=None, purchase_status=None):
    return CommerceOrderLine(
        line_id=1,
        product_id=product_id,
        external_product_id="105721344",
        merchant_sku="105721344",
        title="Товар",
        quantity=2,
        unit_price=Decimal("5000"),
        line_total=Decimal("10000"),
        purchase_request_id=purchase_request_id,
        purchase_status=purchase_status,
    )


def _order(*, status="new", lines=(), original_status="NEW", snapshot_stage=None):
    return CommerceOrder(
        order_id=1,
        external_code="996801988",
        marketplace="kaspi",
        status=status,
        currency="KZT",
        total_amount=Decimal("10000"),
        ordered_at=datetime(2026, 7, 21, tzinfo=UTC),
        delivered_at=None,
        lines=tuple(lines),
        original_status=original_status,
        snapshot_stage=snapshot_stage,
        snapshot_observed_at=(
            datetime(2026, 7, 22, tzinfo=UTC) if snapshot_stage is not None else None
        ),
    )


def test_commerce_order_exposes_procurement_state_separately() -> None:
    line = _line()
    order = _order(lines=(line,))

    assert line.procurement_state == ProcurementState.REQUIRED
    assert order.effective_procurement_state(line) == ProcurementState.REQUIRED
    assert order.units == 2
    assert order.unresolved_lines == 0
    assert order.procurement_required_lines == 1


def test_post_preorder_stages_do_not_show_required_procurement() -> None:
    line = _line()

    for stage in ("ASSEMBLY", "HANDOVER", "SHIPPING", "DELIVERED", "CANCELLED", "RETURNED"):
        order = _order(status="accepted", lines=(line,), snapshot_stage=stage)
        assert order.effective_procurement_state(line) == ProcurementState.NOT_REQUIRED
        assert order.procurement_required_lines == 0


def test_existing_purchase_fact_remains_visible_after_stage_changes() -> None:
    in_progress = _line(purchase_request_id="purchase-1", purchase_status="ordered")
    received = _line(purchase_request_id="purchase-2", purchase_status="received")
    cancelled = _line(purchase_request_id="purchase-3", purchase_status="cancelled")
    order = _order(status="accepted", lines=(in_progress, received, cancelled), snapshot_stage="HANDOVER")

    assert order.effective_procurement_state(in_progress) == ProcurementState.IN_PROGRESS
    assert order.effective_procurement_state(received) == ProcurementState.RECEIVED
    assert order.effective_procurement_state(cancelled) == ProcurementState.CANCELLED


def test_cancelled_delivered_shipping_and_assembly_do_not_request_procurement() -> None:
    line = _line()

    for status in ("cancelled", "delivered", "shipping", "assembly"):
        assert _order(status=status, lines=(line,)).procurement_required_lines == 0


def test_existing_purchase_is_reported_as_in_progress_or_received() -> None:
    assert _line(
        purchase_request_id="purchase-1",
        purchase_status="ordered",
    ).procurement_state == ProcurementState.IN_PROGRESS
    assert _line(
        purchase_request_id="purchase-1",
        purchase_status="received",
    ).procurement_state == ProcurementState.RECEIVED


def test_received_preorder_moves_to_packaging_while_other_kaspi_stages_stay_authoritative() -> None:
    awaiting_preorder = _order(
        status="accepted",
        lines=(_line(purchase_request_id="purchase-1", purchase_status="ordered"),),
        original_status="ACCEPTED_BY_MERCHANT",
    )
    arrived_preorder = _order(
        status="accepted",
        lines=(_line(purchase_request_id="purchase-1", purchase_status="received"),),
        original_status="ACCEPTED_BY_MERCHANT",
    )
    assembly = _order(
        status="assembly",
        lines=(_line(purchase_request_id="purchase-2", purchase_status="requested"),),
        original_status="ASSEMBLY",
    )

    assert awaiting_preorder.stage == CommerceOrderStage.PREORDER
    assert arrived_preorder.stage == CommerceOrderStage.ASSEMBLY
    assert assembly.stage == CommerceOrderStage.ASSEMBLY


def test_mixed_received_and_unreceived_lines_remain_preorder() -> None:
    order = _order(
        status="accepted",
        lines=(
            _line(purchase_request_id="purchase-1", purchase_status="received"),
            CommerceOrderLine(
                line_id=2,
                product_id=2,
                external_product_id="106",
                merchant_sku="SKU-2",
                title="Второй товар",
                quantity=1,
                unit_price=Decimal("1000"),
                line_total=Decimal("1000"),
                purchase_request_id="purchase-2",
                purchase_status="ordered",
            ),
        ),
        original_status="ACCEPTED_BY_MERCHANT",
    )

    assert order.stage == CommerceOrderStage.PREORDER


def test_snapshot_stage_overrides_stale_marketplace_order_status() -> None:
    order = _order(
        status="accepted",
        lines=(_line(),),
        snapshot_stage="HANDOVER",
    )

    assert order.stage == CommerceOrderStage.HANDOVER
    assert order.stage_source == "snapshot"
    assert order.procurement_required_lines == 0


def test_unknown_snapshot_stage_falls_back_to_marketplace_order() -> None:
    order = _order(status="shipping", snapshot_stage="UNSUPPORTED_STAGE")

    assert order.stage == CommerceOrderStage.SHIPPING
    assert order.stage_source == "marketplace_order"


def test_snapshot_terminal_stage_controls_revenue() -> None:
    cancelled = _order(status="accepted", snapshot_stage="CANCELLED")

    assert cancelled.stage == CommerceOrderStage.CANCELLED
    assert cancelled.recognized_revenue == Decimal("0")


def test_new_shipping_delivered_and_cancelled_states_are_exact() -> None:
    line = _line()

    assert _order(status="new", lines=(line,)).stage == CommerceOrderStage.NEW
    assert _order(status="shipping", lines=(line,)).stage == CommerceOrderStage.SHIPPING
    assert _order(status="delivered", lines=(line,)).stage == CommerceOrderStage.DELIVERED
    assert _order(status="cancelled", lines=(line,)).stage == CommerceOrderStage.CANCELLED
    assert _order(status="returned", lines=(line,)).stage == CommerceOrderStage.RETURNED


def test_cancelled_and_returned_orders_are_excluded_from_revenue() -> None:
    orders = (
        _order(status="new", lines=(_line(),)),
        _order(status="delivered", lines=(_line(),)),
        _order(status="cancelled", lines=(_line(product_id=None),)),
        _order(status="returned", lines=(_line(),)),
    )

    summary = CommerceService.summarize(orders)

    assert summary.orders_count == 4
    assert summary.units_count == 8
    assert summary.revenue == Decimal("20000")
    assert summary.active_orders == 1
    assert summary.delivered_orders == 1
    assert summary.cancelled_orders == 2
    assert summary.unresolved_lines == 1
    assert summary.procurement_required_lines == 1
