from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.product_commerce import ProductCommerceAnalyzer, ProductOrderLineFact


def _fact(
    *,
    order_id: int,
    status: str,
    quantity: int,
    total: str,
    days_ago: int,
) -> ProductOrderLineFact:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    return ProductOrderLineFact(
        order_id=order_id,
        status=status,
        quantity=quantity,
        line_total=Decimal(total),
        ordered_at=now - timedelta(days=days_ago),
        delivered_at=now - timedelta(days=max(days_ago - 1, 0)) if status == "delivered" else None,
    )


def test_delivered_revenue_and_profit_exclude_active_and_cancelled_orders() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    analysis = ProductCommerceAnalyzer.analyze(
        [
            _fact(order_id=1, status="delivered", quantity=2, total="10000", days_ago=2),
            _fact(order_id=2, status="shipping", quantity=1, total="5000", days_ago=1),
            _fact(order_id=3, status="cancelled", quantity=1, total="5000", days_ago=1),
        ],
        current_unit_cost=Decimal("3000"),
        cost_source="Ozon",
        now=now,
    )

    week = next(item for item in analysis.windows if item.days == 7)
    assert week.orders_count == 3
    assert week.units_ordered == 3
    assert week.units_delivered == 2
    assert week.active_units == 1
    assert week.cancelled_units == 1
    assert week.delivered_revenue == Decimal("10000.00")
    assert week.estimated_procurement_cost == Decimal("6000.00")
    assert week.estimated_gross_profit_before_fees == Decimal("4000.00")
    assert week.estimated_gross_margin_pct_before_fees == Decimal("40.0")


def test_repeating_sales_recommend_trial_batch() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    analysis = ProductCommerceAnalyzer.analyze(
        [
            _fact(order_id=1, status="delivered", quantity=1, total="5000", days_ago=3),
            _fact(order_id=2, status="delivered", quantity=1, total="5000", days_ago=9),
            _fact(order_id=3, status="shipping", quantity=1, total="5000", days_ago=15),
        ],
        current_unit_cost=Decimal("3000"),
        cost_source="Ozon",
        now=now,
    )

    assert analysis.recommendation.mode == "trial_batch"
    assert analysis.recommendation.target_stock_units >= 2
    assert analysis.recommendation.confidence == "medium"


def test_strong_recent_demand_recommends_stock() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    facts = [
        _fact(
            order_id=index,
            status="delivered",
            quantity=1,
            total="5000",
            days_ago=index % 6,
        )
        for index in range(1, 11)
    ]
    analysis = ProductCommerceAnalyzer.analyze(
        facts,
        current_unit_cost=Decimal("3000"),
        cost_source="Ozon",
        now=now,
    )

    assert analysis.recommendation.mode == "stock"
    assert analysis.recommendation.target_stock_units >= 3
    assert analysis.recommendation.confidence == "high"


def test_profit_is_not_reported_without_cost_basis() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    analysis = ProductCommerceAnalyzer.analyze(
        [_fact(order_id=1, status="delivered", quantity=1, total="5000", days_ago=1)],
        current_unit_cost=None,
        cost_source=None,
        now=now,
    )

    week = next(item for item in analysis.windows if item.days == 7)
    assert week.delivered_revenue == Decimal("5000.00")
    assert week.estimated_procurement_cost is None
    assert week.estimated_gross_profit_before_fees is None
