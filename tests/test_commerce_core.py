from datetime import UTC, datetime
from decimal import Decimal

from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine, CommerceOrderStage, ProcurementState
from backend.app.commerce.service import CommerceService


def _line(*, product_id=1, purchase_request_id=None, purchase_status=None, inventory_allocated_quantity=0):
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
        inventory_allocated_quantity=inventory_allocated_quantity,
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


def test_order_stage_is_already_normalized_by_kaspi_raw_receiver() -> None:
    assert _order(status="preorder").stage == CommerceOrderStage.PREORDER
    assert _order(status="assembly").stage == CommerceOrderStage.ASSEMBLY
    assert _order(status="handover").stage == CommerceOrderStage.HANDOVER
    assert _order(status="shipping").stage == CommerceOrderStage.SHIPPING
    assert _order(status="cancelled").stage == CommerceOrderStage.CANCELLED
    assert _order(status="delivered").stage == CommerceOrderStage.DELIVERED
    assert _order(status="returned").stage == CommerceOrderStage.RETURNED


def test_received_preorder_moves_to_packaging() -> None:
    received = _line(purchase_request_id="purchase-1", purchase_status="received")
    closed = _line(purchase_request_id="purchase-2", purchase_status="closed")
    from_stock = _line(inventory_allocated_quantity=2)
    assert _order(status="preorder", lines=(received,)).stage == CommerceOrderStage.ASSEMBLY
    assert _order(status="preorder", lines=(closed,)).stage == CommerceOrderStage.ASSEMBLY
    assert _order(status="preorder", lines=(from_stock,)).stage == CommerceOrderStage.ASSEMBLY


def test_incomplete_preorder_stays_preorder() -> None:
    ordered = _line(purchase_request_id="purchase-1", purchase_status="ordered")
    received = _line(purchase_request_id="purchase-2", purchase_status="received")
    assert _order(status="preorder", lines=(ordered,)).stage == CommerceOrderStage.PREORDER
    assert _order(status="preorder", lines=(received, ordered)).stage == CommerceOrderStage.PREORDER


def test_order_stage_source_is_official_kaspi_orders_api() -> None:
    assert _order(status="preorder").stage_source == "kaspi_orders_api"


def test_procurement_is_required_only_for_early_order_stages() -> None:
    line = _line()
    assert _order(status="preorder", lines=(line,)).procurement_required_lines == 1
    for stage in ("assembly", "handover", "shipping", "delivered", "cancelled", "returned"):
        order = _order(status=stage, lines=(line,))
        assert order.procurement_required_lines == 0
        assert order.effective_procurement_state(line) == ProcurementState.NOT_REQUIRED


def test_existing_purchase_state_is_preserved() -> None:
    ordered = _line(purchase_request_id="purchase-1", purchase_status="ordered")
    received = _line(purchase_request_id="purchase-2", purchase_status="received")
    cancelled = _line(purchase_request_id="purchase-3", purchase_status="cancelled")
    assert ordered.procurement_state == ProcurementState.IN_PROGRESS
    assert received.procurement_state == ProcurementState.RECEIVED
    assert cancelled.procurement_state == ProcurementState.CANCELLED


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
