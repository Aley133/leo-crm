from types import SimpleNamespace
from decimal import Decimal

from backend.app.fast_dumping_offer_runtime import _clamp_preorder, _supplier_decision
from tools.kaspi_fast_offer_runtime import _matches, _parse_offer


def test_merchant_bff_parser_uses_exact_sku_and_store():
    payload = {
        "data": [
            {
                "sku": "OTHER",
                "available": True,
                "availabilities": [{"storeId": "store", "stockCount": 99, "preOrder": 0, "available": "yes"}],
                "cityPrices": [{"cityId": "196220100", "value": 1}],
            },
            {
                "sku": "SKU-1",
                "available": True,
                "operationType": "IN_PROGRESS",
                "processed": False,
                "appliedBeforeDateTime": "2026-08-23T16:29:13",
                "availabilities": [
                    {"storeId": "wrong", "stockCount": 26, "preOrder": 10, "available": "yes"},
                    {"storeId": "store", "stockCount": 5, "preOrder": 6, "available": "yes"},
                ],
                "cityPrices": [{"cityId": "196220100", "value": 39999}],
            },
        ]
    }
    state = _parse_offer(
        payload=payload,
        sku="SKU-1",
        store_id="store",
        city_id="196220100",
        query_mode="active",
    )
    assert state is not None
    assert state.stock_count == 5
    assert state.preorder_days == 6
    assert state.price_kzt == 39999
    assert state.pending is True


def test_offer_match_supports_zero_state_and_preorder():
    zero = _parse_offer(
        payload={
            "data": [{
                "sku": "SKU",
                "available": False,
                "availabilities": [{"storeId": "S", "stockCount": 0, "preOrder": 0, "available": "no"}],
                "cityPrices": [{"cityId": "C", "value": 1000}],
            }]
        },
        sku="SKU",
        store_id="S",
        city_id="C",
        query_mode="inactive",
    )
    assert zero is not None
    assert _matches(zero, mode="off", stock=0, preorder=0, price=1000)

    preorder = _parse_offer(
        payload={
            "data": [{
                "sku": "SKU",
                "available": True,
                "availabilities": [{"storeId": "S", "stockCount": 5, "preOrder": 8, "available": "yes"}],
                "cityPrices": [{"cityId": "C", "value": 1000}],
            }]
        },
        sku="SKU",
        store_id="S",
        city_id="C",
        query_mode="active",
    )
    assert preorder is not None
    assert _matches(preorder, mode="preorder", stock=5, preorder=8, price=1000)


def test_supplier_decision_uses_monitor_delivery_and_virtual_stock():
    state = SimpleNamespace(
        competitor_price_kzt=Decimal("10000"),
        own_price_kzt=Decimal("10500"),
    )
    policy = SimpleNamespace(
        minimum_profit_kzt=Decimal("1000"),
        undercut_step_kzt=1,
        allow_price_raise=True,
    )
    source = SimpleNamespace(
        unit_cost_kzt=Decimal("5000"),
        delivery_days=8,
        name="Ozon",
    )
    decision = _supplier_decision(state=state, policy=policy, source=source)
    assert decision["fulfillment_mode"] == "preorder"
    assert decision["stock_count"] == 5
    assert decision["preorder_days"] == 8
    assert Decimal(decision["target_price_kzt"]) >= Decimal(decision["safe_floor_kzt"])


def test_preorder_is_bounded_for_supplier_monitoring():
    assert _clamp_preorder(-5) == 0
    assert _clamp_preorder(3) == 3
    assert _clamp_preorder(999) == 60
