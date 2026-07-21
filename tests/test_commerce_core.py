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


def _order(*, status="new", lines=(), original_status="NEW"):
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
    )


def test_commerce_order_exposes_operational_procurement_state() -> None:
    line = _line()
    order = _order(lines=(line,))

    assert line.procurement_state == ProcurementState.REQUIRED
    assert order.units == 2
    assert order.unresolved_lines == 0
    assert order.procurement_required_lines == 1


def test_cancelled_and_delivered_orders_do_not_request_procurement() -> None:
    line = _line()

    assert _order(status="cancelled", lines=(line,)).procurement_required_lines == 0
    assert _order(status="delivered", lines=(line,)).procurement_required_lines == 0


def test_existing_purchase_is_reported_as_in_progress_or_received() -> None:
    assert _line(
        purchase_request_id="purchase-1",
        purchase_status="ordered",
    ).procurement_state == ProcurementState.IN_PROGRESS
    assert _line(
        purchase_request_id="purchase-1",
        purchase_status="received",
    ).procurement_state == ProcurementState.RECEIVED


def test_accepted_order_without_stock_evidence_remains_accepted() -> None:
    order = _order(status="accepted", lines=(_line(),), original_status="ACCEPTED_BY_MERCHANT")

    assert order.stage == CommerceOrderStage.ACCEPTED


def test_purchase_in_progress_moves_accepted_order_to_preorder() -> None:
    order = _order(
        status="accepted",
        lines=(_line(purchase_request_id="purchase-1", purchase_status="ordered"),),
        original_status="ACCEPTED_BY_MERCHANT",
    )

    assert order.stage == CommerceOrderStage.PREORDER


def test_received_procurement_moves_accepted_order_to_assembly() -> None:
    order = _order(
        status="accepted",
        lines=(_line(purchase_request_id="purchase-1", purchase_status="received"),),
        original_status="ACCEPTED_BY_MERCHANT",
    )

    assert order.stage == CommerceOrderStage.ASSEMBLY


def test_shipping_and_delivered_states_are_authoritative() -> None:
    line = _line()

    assert _order(status="shipping", lines=(line,)).stage == CommerceOrderStage.SHIPPING
    assert _order(status="delivered", lines=(line,)).stage == CommerceOrderStage.DELIVERED


def test_commerce_summary_separates_active_delivered_and_cancelled_orders() -> None:
    orders = (
        _order(status="new", lines=(_line(),)),
        _order(status="delivered", lines=(_line(),)),
        _order(status="cancelled", lines=(_line(product_id=None),)),
    )

    summary = CommerceService.summarize(orders)

    assert summary.orders_count == 3
    assert summary.units_count == 6
    assert summary.revenue == Decimal("30000")
    assert summary.active_orders == 1
    assert summary.delivered_orders == 1
    assert summary.cancelled_orders == 1
    assert summary.unresolved_lines == 1
    assert summary.procurement_required_lines == 1
