from decimal import Decimal
from types import SimpleNamespace

from backend.app.fast_dumping_preorder_position import decide_preorder_position
from backend.app.fast_dumping_supplier_pricing import _supplier_decision


def _offers(*prices: int) -> list[dict]:
    return [
        {
            "merchant_id": f"seller-{index}",
            "merchant_name": f"Seller {index}",
            "price_kzt": price,
            "is_own": False,
        }
        for index, price in enumerate(prices, start=1)
    ]


def test_default_fourth_place_uses_highest_price_before_fourth_external_offer() -> None:
    decision = decide_preorder_position(
        own_price_kzt=Decimal("9500"),
        safe_floor_kzt=Decimal("8000"),
        target_position=4,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=_offers(9000, 10000, 11000, 12000, 13000),
    )

    assert decision.target_price_kzt == Decimal("11999.00")
    assert decision.desired_position == 4
    assert decision.estimated_position == 4
    assert decision.exact is True
    assert decision.anchor_price_kzt == Decimal("12000.00")


def test_position_can_be_selected_higher_or_lower() -> None:
    offers = _offers(9000, 10000, 11000, 12000, 13000, 14000, 15000, 16000)
    second = decide_preorder_position(
        own_price_kzt=Decimal("12000"),
        safe_floor_kzt=Decimal("7000"),
        target_position=2,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=offers,
    )
    eighth = decide_preorder_position(
        own_price_kzt=Decimal("12000"),
        safe_floor_kzt=Decimal("7000"),
        target_position=8,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=offers,
    )

    assert second.target_price_kzt == Decimal("9999.00")
    assert second.estimated_position == 2
    assert second.exact is True
    assert eighth.target_price_kzt == Decimal("15999.00")
    assert eighth.estimated_position == 8
    assert eighth.exact is True


def test_preorder_position_does_not_count_another_owned_shop_as_external() -> None:
    offers = _offers(9000, 10000, 11000, 12000)
    offers[0]["merchant_name"] = "LeoXpress"
    offers[0]["is_owned_group"] = True
    offers[0]["is_owned_peer"] = True

    decision = decide_preorder_position(
        own_price_kzt=Decimal("9500"),
        safe_floor_kzt=Decimal("7000"),
        target_position=2,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=offers,
    )

    assert decision.external_sellers == 3
    assert decision.target_price_kzt == Decimal("10999.00")


def test_floor_wins_over_requested_position() -> None:
    decision = decide_preorder_position(
        own_price_kzt=Decimal("13000"),
        safe_floor_kzt=Decimal("12500"),
        target_position=2,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=_offers(9000, 10000, 11000, 12000, 13000),
    )

    assert decision.target_price_kzt == Decimal("12500.00")
    assert decision.estimated_position == 5
    assert decision.exact is False
    assert "floor" in decision.reason


def test_equal_neighbor_prices_become_best_effort_instead_of_fake_exact_rank() -> None:
    decision = decide_preorder_position(
        own_price_kzt=Decimal("10000"),
        safe_floor_kzt=Decimal("7000"),
        target_position=4,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=_offers(9000, 10000, 11000, 11000, 13000),
    )

    assert decision.target_price_kzt == Decimal("11000.00")
    assert decision.exact is False
    assert "ничья" in decision.reason


def test_too_few_sellers_returns_closest_available_position() -> None:
    decision = decide_preorder_position(
        own_price_kzt=Decimal("10000"),
        safe_floor_kzt=Decimal("7000"),
        target_position=8,
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        market_offers=_offers(9000, 10000, 11000),
    )

    assert decision.target_price_kzt == Decimal("11001.00")
    assert decision.estimated_position == 4
    assert decision.exact is False


def test_supplier_decision_uses_policy_position_and_keeps_preorder_fulfillment() -> None:
    state = SimpleNamespace(
        own_price_kzt=Decimal("9500"),
        competitor_price_kzt=Decimal("9000"),
        offers_json=_offers(9000, 10000, 11000, 12000, 13000),
        page_visible_price_kzt=None,
    )
    policy = SimpleNamespace(
        minimum_profit_kzt=Decimal("1000"),
        undercut_step_kzt=1,
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("100"),
        preorder_target_position=4,
    )
    source = SimpleNamespace(
        unit_cost_kzt=Decimal("7000"),
        delivery_days=6,
        name="Ozon",
    )

    decision = _supplier_decision(state=state, policy=policy, source=source)

    assert decision["target_price_kzt"] == "11999.00"
    assert decision["stock_count"] == 5
    assert decision["preorder_days"] == 6
    assert decision["fulfillment_mode"] == "preorder"
    assert decision["preorder_target_position"] == 4
    assert decision["preorder_estimated_position"] == 4
    assert decision["preorder_position_exact"] is True
