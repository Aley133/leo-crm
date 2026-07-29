from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine
from backend.app.commerce.service import CommerceService
from backend.app.revenue_api import _append_new_orders
from backend.app.revenue_models import DailyRevenueSnapshot


def _order(order_id: int, *, amount: str, cost: str) -> CommerceOrder:
    total = Decimal(amount)
    return CommerceOrder(
        order_id=order_id,
        external_code=str(order_id),
        marketplace="kaspi",
        marketplace_account_id=1,
        status="assembly",
        currency="KZT",
        total_amount=total,
        ordered_at=None,
        delivered_at=None,
        lines=(
            CommerceOrderLine(
                line_id=order_id,
                product_id=order_id,
                external_product_id=str(order_id),
                merchant_sku=str(order_id),
                title=f"Product {order_id}",
                quantity=1,
                unit_price=total,
                line_total=total,
                purchase_request_id=None,
                purchase_status=None,
                procurement_unit_cost=Decimal(cost),
                inventory_allocated_quantity=1,
            ),
        ),
    )


def _snapshot() -> DailyRevenueSnapshot:
    return DailyRevenueSnapshot(
        marketplace_account_id=1,
        business_date=date(2026, 7, 29),
        timezone="Asia/Almaty",
        source_stage="assembly",
        orders_count=1,
        units_count=1,
        revenue=Decimal("10000.00"),
        net_profit=Decimal("3000.00"),
        margin_pct=Decimal("30.0000"),
        order_ids=[101],
    )


def test_revenue_capture_appends_only_orders_not_saved_before() -> None:
    snapshot = _snapshot()
    already_saved = _order(101, amount="10000", cost="4000")
    new_order = _order(102, amount="8000", cost="3000")
    captured_at = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
    added_summary = CommerceService.summarize((new_order,))

    changed = _append_new_orders(
        snapshot,
        [already_saved, new_order],
        captured_at=captured_at,
    )

    assert changed is True
    assert snapshot.order_ids == [101, 102]
    assert snapshot.orders_count == 2
    assert snapshot.units_count == 2
    assert snapshot.revenue == Decimal("10000.00") + added_summary.revenue
    assert snapshot.net_profit == Decimal("3000.00") + added_summary.confirmed_net_profit
    assert snapshot.margin_pct == (
        snapshot.net_profit * Decimal("100") / snapshot.revenue
    ).quantize(Decimal("0.0001"))
    assert snapshot.captured_at == captured_at


def test_repeated_revenue_capture_is_idempotent() -> None:
    snapshot = _snapshot()
    already_saved = _order(101, amount="10000", cost="4000")
    original_captured_at = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    snapshot.captured_at = original_captured_at

    changed = _append_new_orders(
        snapshot,
        [already_saved],
        captured_at=datetime(2026, 7, 29, 14, 0, tzinfo=UTC),
    )

    assert changed is False
    assert snapshot.order_ids == [101]
    assert snapshot.orders_count == 1
    assert snapshot.units_count == 1
    assert snapshot.revenue == Decimal("10000.00")
    assert snapshot.net_profit == Decimal("3000.00")
    assert snapshot.margin_pct == Decimal("30.0000")
    assert snapshot.captured_at == original_captured_at
