from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.kaspi_order_board import classify_kaspi_order


def order(
    *,
    pre_order=False,
    delivery_cost=0,
    status="ACCEPTED_BY_MERCHANT",
    state="KASPI_DELIVERY",
    planned=None,
    actual=None,
):
    return {
        "code": "100",
        "preOrder": pre_order,
        "deliveryCostForSeller": delivery_cost,
        "status": status,
        "state": state,
        "creationDate": 1784723114744,
        "courierTransmissionPlanningDate": planned,
        "courierTransmissionDate": actual,
    }


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_preorder_when_cost_zero() -> None:
    assert classify_kaspi_order(order(pre_order=True)) == "preorder"


def test_packing_when_cost_zero() -> None:
    assert classify_kaspi_order(order(pre_order=False)) == "assembly"


def test_handoff_when_cost_positive_and_planned_today_before_21() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 15, 0, tzinfo=zone)
    planned = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    assert classify_kaspi_order(order(delivery_cost=57, planned=ms(planned)), now=now) == "handover"


def test_delivery_when_today_after_21() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 21, 1, tzinfo=zone)
    planned = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    assert classify_kaspi_order(order(delivery_cost=57, planned=ms(planned)), now=now) == "shipping"


def test_delivery_when_planned_yesterday() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    planned = datetime(2026, 7, 21, 10, 0, tzinfo=zone)
    assert classify_kaspi_order(order(delivery_cost=57, planned=ms(planned)), now=now) == "shipping"


def test_actual_transmission_has_priority() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    actual = datetime(2026, 7, 22, 9, 0, tzinfo=zone)
    assert classify_kaspi_order(order(delivery_cost=57, actual=ms(actual)), now=now) == "shipping"


def test_positive_cost_without_dates_stays_handoff_on_observation_day() -> None:
    zone = ZoneInfo("Asia/Almaty")
    observed_same_day = datetime(2026, 7, 22, 20, 0, tzinfo=zone)
    assert (
        classify_kaspi_order(order(delivery_cost=57), now=observed_same_day)
        == "handover"
    )


def test_terminal_statuses_have_priority() -> None:
    assert classify_kaspi_order(order(pre_order=True, delivery_cost=57, status="CANCELLING")) == "cancelling"
    assert classify_kaspi_order(order(pre_order=True, delivery_cost=57, status="CANCELLED")) == "cancelled"
    assert classify_kaspi_order(order(pre_order=True, delivery_cost=57, status="COMPLETED")) == "delivered"


def test_first_import_old_positive_cost_is_delivery() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 23, 10, 0, tzinfo=zone)
    item = order(delivery_cost=57)
    item["creationDate"] = ms(datetime(2026, 7, 22, 18, 0, tzinfo=zone))
    assert classify_kaspi_order(item, now=now) == "shipping"


def test_new_order_after_cutoff_remains_handoff() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 22, 0, tzinfo=zone)
    item = order(delivery_cost=57)
    item["creationDate"] = ms(datetime(2026, 7, 22, 21, 30, tzinfo=zone))
    assert classify_kaspi_order(item, now=now) == "handover"


def test_history_transition_after_cutoff_waits_until_next_day_cutoff() -> None:
    zone = ZoneInfo("Asia/Almaty")
    started = datetime(2026, 7, 22, 21, 30, tzinfo=zone)
    record = {"transfer_started_at": started.isoformat()}
    item = order(delivery_cost=57)
    assert classify_kaspi_order(
        item,
        now=datetime(2026, 7, 23, 20, 59, tzinfo=zone),
        history_record=record,
    ) == "handover"
    assert classify_kaspi_order(
        item,
        now=datetime(2026, 7, 23, 21, 0, tzinfo=zone),
        history_record=record,
    ) == "shipping"
