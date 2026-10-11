from decimal import Decimal

from backend.app.accounting_planning import build_purchase_plan


def plan(current=None, previous=None, stock=5, incoming=3, price=100):
    return build_purchase_plan(current=current or {}, previous=previous or {},
        inventory={"items": [{"product_id": 1, "name": "Товар", "on_hand_units": stock,
            "incoming_units": incoming, "last_purchase_price": price, "inventory_value": stock * 100}]},
        delivered_orders=60)


def sales(units=30, profit=3000, unpriced=0):
    return {1: {"product_id": 1, "delivered_units": units, "orders": set(range(15)),
                "known_net_profit": Decimal(profit), "unpriced_units": unpriced}}


def test_replenishment_deducts_stock_and_transit_without_scaling_to_goal():
    result = plan(current=sales())
    assert result["buy"][0]["purchase_units"] == 22
    assert result["purchase_amount"] == 2200
    assert result["orders_per_day"] == 2
    assert result["goals"] == [{"target": 100, "gap": Decimal("98")}, {"target": 200, "gap": Decimal("198")}]
    assert result["buy"][0]["orders_per_day"] == Decimal("0.5")


def test_transit_covers_demand():
    assert plan(current=sales(), incoming=40)["buy"] == []


def test_lost_sales_with_stock_or_transit_require_review_not_purchase():
    for stock, incoming in ((5, 0), (0, 30)):
        result = plan(previous=sales(), stock=stock, incoming=incoming)
        assert result["buy"] == []
        assert result["recover"][0]["purchase_units"] == 0
        assert result["purchase_amount"] == 0


def test_lost_sales_purchase_is_separate_conditional_estimate():
    result = plan(previous=sales(), stock=0, incoming=0)
    assert result["purchase_amount"] == 0
    assert result["recovery_amount"] == 3000


def test_unknown_cost_and_loss_do_not_trigger_purchase():
    for stats, price in ((sales(unpriced=1), 100), (sales(profit=-100), 100), (sales(), None)):
        result = plan(current=stats, price=price)
        assert not result["buy"]
        assert result["hold"][0]["purchase_units"] == 0


def test_no_sales_freezes_stock():
    assert len(plan()["hold"]) == 1
