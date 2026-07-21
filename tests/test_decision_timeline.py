from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.decision_timeline import (
    DecisionTimelineProjector,
    TimelineBinding,
    TimelineObservation,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def binding(binding_id: int, supplier_product_id: int, code: str) -> TimelineBinding:
    return TimelineBinding(
        binding_id=binding_id,
        supplier_product_id=supplier_product_id,
        supplier_code=code,
        supplier_name=code.upper(),
        is_primary=False,
        priority=100,
    )


def observation(
    observation_id: int,
    supplier_product_id: int,
    *,
    minutes: int,
    price: str | None,
    delivery_days: int | None,
    available: bool | None = True,
) -> TimelineObservation:
    return TimelineObservation(
        observation_id=observation_id,
        supplier_product_id=supplier_product_id,
        price=None if price is None else Decimal(price),
        currency="KZT",
        available=available,
        delivery_days=delivery_days,
        observed_at=NOW + timedelta(minutes=minutes),
    )


def test_timeline_rebuilds_leader_changes_from_observations() -> None:
    timeline = DecisionTimelineProjector.project(
        [binding(1, 101, "ozon"), binding(2, 202, "wb")],
        [
            observation(1, 101, minutes=0, price="4000", delivery_days=5),
            observation(2, 202, minutes=1, price="4500", delivery_days=2),
            observation(3, 101, minutes=2, price="3000", delivery_days=2),
        ],
    )

    newest = timeline[0]
    assert newest.event_type == "leader_changed"
    assert newest.leader_supplier_code == "ozon"
    assert newest.previous_supplier_code == "wb"
    assert newest.price_delta == Decimal("-1500")
    assert "дешевле" in newest.reason
    assert timeline[-1].event_type == "initial_leader"


def test_timeline_records_loss_of_available_decision() -> None:
    timeline = DecisionTimelineProjector.project(
        [binding(1, 101, "ozon")],
        [
            observation(1, 101, minutes=0, price="3000", delivery_days=1),
            observation(2, 101, minutes=1, price=None, delivery_days=None, available=False),
        ],
    )

    assert timeline[0].event_type == "no_decision"
    assert timeline[0].leader_binding_id is None
    assert timeline[0].previous_supplier_code == "ozon"
    assert timeline[0].confidence == "none"


def test_timeline_can_suppress_reaffirmed_snapshots() -> None:
    timeline = DecisionTimelineProjector.project(
        [binding(1, 101, "ozon")],
        [
            observation(1, 101, minutes=0, price="3000", delivery_days=1),
            observation(2, 101, minutes=1, price="2900", delivery_days=1),
        ],
        include_reaffirmed=False,
    )

    assert len(timeline) == 1
    assert timeline[0].event_type == "initial_leader"
