from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.app.fast_dumping_pricing import decide_fast_price
from tools.kaspi_fast_dumping_scanner import _select_delivery_aware_competitor


def test_lab_rule_skips_399_kzt_cheaper_offer_four_days_slower() -> None:
    """Regression for the localhost case validated before CRM integration."""

    today = date(2026, 8, 21)
    own = {
        "merchantName": "LEO",
        "price": "9999",
        "delivery": "2026-08-22T18:00:00+00:00",
    }
    slow_cheaper = {
        "merchantName": "Slow seller",
        "price": "9600",
        "delivery": "2026-08-26T18:00:00+00:00",
    }
    next_fast_offer = {
        "merchantName": "Fast seller",
        "price": "10020",
        "delivery": "2026-08-22T18:00:00+00:00",
    }

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [slow_cheaper, next_fast_offer],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=3,
        today=today,
    )

    slow = assessments[id(slow_cheaper)]
    assert slow.ignored is True
    assert slow.price_gap_kzt == Decimal("399")
    assert slow.delivery_gap_days == 4
    assert selected is next_fast_offer

    decision = decide_fast_price(
        own_price_kzt=Decimal("9999"),
        competitor_price_kzt=Decimal(str(selected["price"])),
        safe_floor_kzt=Decimal("8000"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )
    assert decision.target_price_kzt == Decimal("10019.00")


def test_lab_rule_keeps_slow_offer_when_price_advantage_exceeds_limit() -> None:
    today = date(2026, 8, 21)
    own = {"price": "9999", "delivery": "2026-08-22T18:00:00+00:00"}
    materially_cheaper = {
        "price": "9498",
        "delivery": "2026-08-26T18:00:00+00:00",
    }

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [materially_cheaper],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=3,
        today=today,
    )

    assessment = assessments[id(materially_cheaper)]
    assert assessment.ignored is False
    assert assessment.price_gap_kzt == Decimal("501")
    assert assessment.delivery_gap_days == 4
    assert selected is materially_cheaper


def test_lab_rule_is_fail_safe_when_delivery_is_unknown() -> None:
    today = date(2026, 8, 21)
    own = {"price": "9999", "delivery": "2026-08-22T18:00:00+00:00"}
    unknown_delivery = {"price": "9600", "kaspiDelivery": True}

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [unknown_delivery],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=3,
        today=today,
    )

    assessment = assessments[id(unknown_delivery)]
    assert assessment.ignored is False
    assert assessment.competitor_delivery_days is None
    assert selected is unknown_delivery
