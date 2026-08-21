from __future__ import annotations

from decimal import Decimal

from backend.app.fast_dumping_pricing import (
    build_delivery_business_plan,
    decide_fast_price,
)


def _offer(
    name: str,
    price: int,
    *,
    delivery_gap: int,
    own_price: int = 8000,
    is_own: bool = False,
) -> dict:
    return {
        "merchant_name": name,
        "price_kzt": str(price),
        "is_own": is_own,
        "delivery_gap_days": None if is_own else delivery_gap,
        "price_gap_kzt": None if is_own else str(own_price - price),
    }


def test_delivery_business_raises_up_to_configured_premium_when_top5_is_safe() -> None:
    offers = [
        _offer("LEO", 8000, delivery_gap=0, is_own=True),
        _offer("Zecar", 8000, delivery_gap=5),
        _offer("Duken", 8100, delivery_gap=5),
        _offer("Fast seller", 9000, delivery_gap=0),
    ]

    decision = decide_fast_price(
        own_price_kzt=Decimal("8000"),
        competitor_price_kzt=Decimal("9000"),
        safe_floor_kzt=Decimal("6000"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
        market_offers=offers,
        delivery_price_premium_kzt=500,
        delivery_advantage_days=3,
        page_visible_price_kzt=Decimal("8000"),
    )

    assert decision.target_price_kzt == Decimal("8500.00")
    assert "Zecar" in decision.reason
    assert "Duken" in decision.reason
    assert "Премия за быструю доставку" in decision.reason


def test_delivery_business_caps_price_to_stay_inside_first_five_sellers() -> None:
    offers = [
        _offer("LEO", 8000, delivery_gap=0, is_own=True),
        _offer("Slow-1", 8000, delivery_gap=5),
        _offer("Slow-2", 8000, delivery_gap=5),
        _offer("Slow-3", 8000, delivery_gap=5),
        _offer("Slow-4", 8000, delivery_gap=5),
        _offer("Slow-5", 8000, delivery_gap=5),
        _offer("Slow-6", 8000, delivery_gap=5),
    ]

    decision = decide_fast_price(
        own_price_kzt=Decimal("8000"),
        competitor_price_kzt=None,
        safe_floor_kzt=Decimal("6000"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
        market_offers=offers,
        delivery_price_premium_kzt=500,
        delivery_advantage_days=3,
        page_visible_price_kzt=Decimal("8000"),
    )

    assert decision.target_price_kzt == Decimal("7999.00")
    assert decision.status == "delivery_advantage"
    assert "TOP-5" in decision.reason
    assert "7999.00" in decision.reason


def test_delivery_business_uses_configured_600_kzt_premium() -> None:
    offers = [
        _offer("LEO", 8000, delivery_gap=0, is_own=True),
        _offer("Slow", 8000, delivery_gap=6),
        _offer("Fast", 9000, delivery_gap=0),
    ]

    plan = build_delivery_business_plan(
        own_price_kzt=Decimal("8000"),
        competitor_price_kzt=Decimal("9000"),
        market_offers=offers,
        delivery_price_premium_kzt=600,
        delivery_advantage_days=3,
        undercut_step_kzt=Decimal("1"),
        page_visible_price_kzt=Decimal("8000"),
    )

    assert plan is not None
    assert plan.premium_ceiling_kzt == Decimal("8600.00")
    assert plan.target_ceiling_kzt == Decimal("8600.00")


def test_delivery_business_does_not_use_untrusted_offer_below_public_price() -> None:
    offers = [
        _offer("LEO", 8400, delivery_gap=0, own_price=8400, is_own=True),
        _offer("Other context", 7000, delivery_gap=8, own_price=8400),
        _offer("Trusted slow", 8372, delivery_gap=4, own_price=8400),
        _offer("Fast", 9000, delivery_gap=0, own_price=8400),
    ]

    plan = build_delivery_business_plan(
        own_price_kzt=Decimal("8400"),
        competitor_price_kzt=Decimal("9000"),
        market_offers=offers,
        delivery_price_premium_kzt=500,
        delivery_advantage_days=3,
        undercut_step_kzt=Decimal("1"),
        page_visible_price_kzt=Decimal("8372"),
    )

    assert plan is not None
    assert plan.premium_ceiling_kzt == Decimal("8872.00")
    assert "Other context" not in plan.ignored_names
    assert "Trusted slow" in plan.ignored_names
