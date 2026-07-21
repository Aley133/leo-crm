from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.supplier_intelligence import BestOfferEngine, SupplierCandidate


def candidate(
    binding_id: int,
    *,
    code: str,
    price: str | None,
    available: bool | None = True,
    delivery_days: int | None = None,
    is_primary: bool = False,
    priority: int = 100,
    checked_hours_ago: int = 1,
) -> SupplierCandidate:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    return SupplierCandidate(
        binding_id=binding_id,
        supplier_product_id=binding_id * 10,
        supplier_code=code,
        supplier_name=code.upper(),
        price=None if price is None else Decimal(price),
        currency="KZT",
        available=available,
        delivery_days=delivery_days,
        is_primary=is_primary,
        priority=priority,
        last_checked_at=now - timedelta(hours=checked_hours_ago),
    )


def test_best_offer_engine_prefers_better_price_and_delivery() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    decision = BestOfferEngine.decide(
        [
            candidate(1, code="ozon", price="3156", delivery_days=2),
            candidate(2, code="wb", price="4530", delivery_days=5),
        ],
        now=now,
    )

    assert decision.best is not None
    assert decision.best.supplier_code == "ozon"
    assert decision.runner_up is not None
    assert decision.runner_up.supplier_code == "wb"
    assert decision.score_gap is not None
    assert decision.best.total_score > decision.runner_up.total_score
    assert "Самая низкая подтверждённая цена" in decision.best.reasons
    assert "Доставка за 2 дн." in decision.best.reasons


def test_unavailable_or_unpriced_offer_is_not_eligible() -> None:
    decision = BestOfferEngine.decide(
        [
            candidate(1, code="ozon", price="3156", available=False, delivery_days=2),
            candidate(2, code="wb", price=None, available=True, delivery_days=1),
        ]
    )

    assert decision.best is None
    assert decision.runner_up is None
    assert decision.confidence == "none"
    assert all(score.eligible is False for score in decision.ranked)


def test_primary_binding_is_preference_not_hard_override() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    decision = BestOfferEngine.decide(
        [
            candidate(1, code="ozon", price="3000", delivery_days=1),
            candidate(2, code="wb", price="6000", delivery_days=8, is_primary=True),
        ],
        now=now,
    )

    assert decision.best is not None
    assert decision.best.supplier_code == "ozon"


def test_score_is_explainable_and_bounded() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    score = BestOfferEngine.decide(
        [candidate(1, code="ozon", price="3156", delivery_days=0, is_primary=True, priority=0)],
        now=now,
    ).best

    assert score is not None
    assert score.total_score == Decimal("100.00")
    assert score.price_score == Decimal("55")
    assert score.delivery_score == Decimal("25.00")
    assert score.preference_score == Decimal("15")
    assert score.freshness_score == Decimal("5")


def test_single_offer_has_low_confidence_even_with_perfect_score() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    decision = BestOfferEngine.decide(
        [candidate(1, code="ozon", price="3156", delivery_days=0, is_primary=True, priority=0)],
        now=now,
    )

    assert decision.confidence == "low"
    assert decision.runner_up is None
    assert decision.score_gap is None
    assert "Решение основано только на одном доступном предложении" in decision.warnings


def test_clear_score_gap_produces_high_confidence() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    decision = BestOfferEngine.decide(
        [
            candidate(1, code="ozon", price="3000", delivery_days=0, is_primary=True, priority=0),
            candidate(2, code="wb", price="6000", delivery_days=10),
        ],
        now=now,
    )

    assert decision.confidence == "high"
    assert decision.score_gap is not None
    assert decision.score_gap >= BestOfferEngine.HIGH_CONFIDENCE_GAP
    assert decision.warnings == ()


def test_close_scores_produce_low_confidence_warning() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    decision = BestOfferEngine.decide(
        [
            candidate(1, code="ozon", price="3000", delivery_days=2),
            candidate(2, code="wb", price="3050", delivery_days=2),
        ],
        now=now,
    )

    assert decision.confidence == "low"
    assert decision.score_gap is not None
    assert decision.score_gap < BestOfferEngine.MEDIUM_CONFIDENCE_GAP
    assert "Первое и второе предложения имеют близкий рейтинг" in decision.warnings
