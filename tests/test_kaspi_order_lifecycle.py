from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.kaspi_order_board import classify_kaspi_order
from backend.app.kaspi_order_payload import canonicalize_kaspi_order_payload
from backend.app.marketplace_import import normalize_kaspi_order


def _attrs(**overrides):
    attrs = {
        "code": "1000025629",
        "state": "KASPI_DELIVERY",
        "status": "ACCEPTED_BY_MERCHANT",
        "creationDate": 1784723114744,
        "deliveryCostForSeller": 0,
        "preOrder": False,
        "entries": [],
        "totalPrice": 9999,
    }
    attrs.update(overrides)
    return attrs


def _normalized(**overrides) -> str:
    payload = {"id": "order-1", "attributes": _attrs(**overrides)}
    return normalize_kaspi_order(canonicalize_kaspi_order_payload(payload)).status


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_preorder_is_defined_by_explicit_flag_when_logistics_cost_is_zero() -> None:
    assert classify_kaspi_order(_attrs(preOrder=True, deliveryCostForSeller=0)) == "preorder"
    assert _normalized(preOrder=True, deliveryCostForSeller=0) == "accepted"


def test_regular_accepted_order_is_packaging_when_logistics_cost_is_zero() -> None:
    assert classify_kaspi_order(_attrs(preOrder=False, deliveryCostForSeller=0)) == "assembly"
    assert _normalized(preOrder=False, deliveryCostForSeller=0) == "assembly"


def test_positive_logistics_cost_before_cutoff_is_handover() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 15, 0, tzinfo=zone)
    planned = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    attrs = _attrs(deliveryCostForSeller=57, courierTransmissionPlanningDate=_ms(planned))
    assert classify_kaspi_order(attrs, now=now) == "handover"


def test_positive_logistics_cost_after_cutoff_is_shipping() -> None:
    zone = ZoneInfo("Asia/Almaty")
    now = datetime(2026, 7, 22, 21, 1, tzinfo=zone)
    planned = datetime(2026, 7, 22, 10, 0, tzinfo=zone)
    attrs = _attrs(deliveryCostForSeller=57, courierTransmissionPlanningDate=_ms(planned))
    assert classify_kaspi_order(attrs, now=now) == "shipping"


def test_actual_courier_transmission_is_shipping() -> None:
    zone = ZoneInfo("Asia/Almaty")
    actual = datetime(2026, 7, 22, 9, 0, tzinfo=zone)
    attrs = _attrs(deliveryCostForSeller=57, courierTransmissionDate=_ms(actual))
    assert classify_kaspi_order(attrs) == "shipping"
    assert _normalized(deliveryCostForSeller=57, courierTransmissionDate=_ms(actual)) == "shipping"


def test_terminal_statuses_are_authoritative() -> None:
    assert classify_kaspi_order(_attrs(status="CANCELLED", preOrder=True)) == "cancelled"
    assert classify_kaspi_order(_attrs(status="COMPLETED", preOrder=True)) == "delivered"
    assert classify_kaspi_order(_attrs(status="RETURNED", preOrder=True)) == "returned"


def test_graphql_only_flags_are_not_part_of_the_new_order_model() -> None:
    attrs = _attrs(
        preOrder=True,
        deliveryCostForSeller=0,
        delivery={"kdAssembled": True, "kdTransmittedToCourier": True},
        markers=[{"marker": "CARGO_ASSEMBLED"}],
    )
    assert classify_kaspi_order(attrs) == "preorder"
