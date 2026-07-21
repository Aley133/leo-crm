from datetime import UTC, datetime
from decimal import Decimal

from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine, CommerceOrderStage
from backend.app.kaspi_order_payload import canonicalize_kaspi_order_payload
from backend.app.marketplace_import import normalize_kaspi_order


def _payload(*, state: str = "KASPI_DELIVERY", status: str = "ACCEPTED_BY_MERCHANT", **facts) -> dict:
    attributes = {
        "code": "1000025629",
        "state": state,
        "status": status,
        "creationDate": "2026-07-18T10:00:00Z",
        "totalPrice": 9999,
        "entries": [],
    }
    attributes.update(facts)
    return {"id": "order-1", "attributes": attributes}


def _line(*, purchase_request_id=None, purchase_status=None) -> CommerceOrderLine:
    return CommerceOrderLine(
        line_id=1,
        product_id=1,
        external_product_id="123",
        merchant_sku="SKU-1",
        title="Товар",
        quantity=1,
        unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
        purchase_request_id=purchase_request_id,
        purchase_status=purchase_status,
    )


def _order(*, status: str, line: CommerceOrderLine) -> CommerceOrder:
    return CommerceOrder(
        order_id=1,
        external_code="1000025629",
        marketplace="kaspi",
        status=status,
        currency="KZT",
        total_amount=Decimal("9999"),
        ordered_at=datetime(2026, 7, 18, tzinfo=UTC),
        delivered_at=None,
        lines=(line,),
    )


def _normalized(payload: dict) -> str:
    return normalize_kaspi_order(canonicalize_kaspi_order_payload(payload)).status


def test_kaspi_delivery_bucket_never_defines_stage_by_itself() -> None:
    assert _normalized(_payload(status="APPROVED_BY_BANK")) == "new"


def test_preorder_candidate_is_detected_from_explicit_preorder_flag() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=True,
        assembled=False,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784818800000,
            "waybill": None,
        },
    )

    canonical = canonicalize_kaspi_order_payload(payload)

    assert canonical["attributes"]["marketplaceStatus"] == "ACCEPTED_BY_MERCHANT"
    assert _normalized(payload) == "accepted"
    assert _order(status="accepted", line=_line()).stage == CommerceOrderStage.PREORDER


def test_arrived_preorder_is_packaging_in_leo_even_if_kaspi_candidate_is_unchanged() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=True,
        assembled=False,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784732400000,
            "waybill": None,
        },
    )

    assert _normalized(payload) == "accepted"
    assert _order(
        status="accepted",
        line=_line(purchase_request_id="purchase-1", purchase_status="received"),
    ).stage == CommerceOrderStage.ASSEMBLY


def test_normal_merchant_acceptance_is_packaging() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        assembled=False,
        kaspiDelivery={"courierTransmissionDate": None},
    )

    assert _normalized(payload) == "assembly"
    assert _order(status="assembly", line=_line()).stage == CommerceOrderStage.ASSEMBLY


def test_assembled_order_without_actual_transmission_is_handover() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        assembled=True,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784732400000,
            "waybill": "https://kaspi.kz/shop/api/waybill/example",
            "waybillNumber": "395637626",
        },
    )

    assert _normalized(payload) == "handover"
    assert _order(status="handover", line=_line()).stage == CommerceOrderStage.HANDOVER


def test_actual_courier_transmission_wins_over_stale_preorder_and_assembled_flags() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=True,
        assembled=True,
        kaspiDelivery={
            "courierTransmissionDate": 1784642859000,
            "courierTransmissionPlanningDate": 1784642859000,
        },
    )

    assert _normalized(payload) == "shipping"
    assert _order(status="shipping", line=_line()).stage == CommerceOrderStage.SHIPPING


def test_legacy_actual_shipment_field_means_handed_to_kaspi_delivery() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        shipmentDate="2026-07-21T12:00:00Z",
        plannedShipmentDate="2026-07-21T20:00:00Z",
    )

    assert _normalized(payload) == "shipping"


def test_planned_transmission_deadline_alone_does_not_mean_shipping() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        assembled=False,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784642859000,
        },
    )

    assert _normalized(payload) == "assembly"


def test_cancelled_completed_and_returned_are_authoritative() -> None:
    assert _normalized(
        _payload(
            status="CANCELLED",
            preOrder=True,
            assembled=True,
            kaspiDelivery={"courierTransmissionDate": 1784642859000},
        )
    ) == "cancelled"
    assert _normalized(_payload(status="COMPLETED", preOrder=True)) == "delivered"
    assert _normalized(_payload(status="KASPI_DELIVERY_RETURN_REQUESTED")) == "returned"


def test_arrival_fact_only_advances_preorder_and_never_rewrites_physical_kaspi_stage() -> None:
    arrived_preorder = _order(
        status="accepted",
        line=_line(purchase_request_id="pr-1", purchase_status="received"),
    )
    shipping_with_requested_purchase = _order(
        status="shipping",
        line=_line(purchase_request_id="pr-2", purchase_status="requested"),
    )

    assert arrived_preorder.stage == CommerceOrderStage.ASSEMBLY
    assert shipping_with_requested_purchase.stage == CommerceOrderStage.SHIPPING


def test_seller_graphql_preorder_waiting_for_arrival_is_preorder() -> None:
    payload = _payload(
        state="KASPI_DELIVERY_WAIT_FOR_POINT_DELIVERY",
        status="ACCEPTED",
        preOrder=True,
        delivery={
            "kdAssembled": False,
            "kdTransmittedToCourier": False,
            "isOrderArrived": False,
        },
        orderSteps=[
            {"step": "PRE_ORDER", "actualTime": None, "plannedTime": "2026-07-22T18:59:50.999Z"},
        ],
    )

    assert _normalized(payload) == "accepted"


def test_seller_graphql_cargo_assembly_is_packaging() -> None:
    payload = _payload(
        state="KASPI_DELIVERY_CARGO_ASSEMBLY",
        status="PRE_ORDERED",
        preOrder=True,
        delivery={
            "kdAssembled": False,
            "kdTransmittedToCourier": False,
            "isOrderArrived": True,
        },
        orderSteps=[
            {"step": "PRE_ORDER", "actualTime": "2026-07-21T16:07:04.986Z"},
        ],
    )

    assert _normalized(payload) == "assembly"


def test_seller_graphql_wait_for_courier_is_handover() -> None:
    payload = _payload(
        state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        status="ASSEMBLED",
        preOrder=False,
        delivery={
            "kdAssembled": True,
            "kdTransmittedToCourier": False,
            "isOrderArrived": True,
        },
        markers=[{"marker": "CARGO_ASSEMBLED"}],
    )

    assert _normalized(payload) == "handover"


def test_seller_graphql_transmitted_is_shipping() -> None:
    payload = _payload(
        state="KASPI_DELIVERY_TRANSMITTED",
        status="TRANSMITTED",
        preOrder=True,
        delivery={
            "kdAssembled": True,
            "kdTransmittedToCourier": True,
            "isOrderArrived": True,
        },
        orderSteps=[
            {"step": "TRANSMISSION", "actualTime": "2026-07-21T14:07:39.000Z"},
        ],
    )

    assert _normalized(payload) == "shipping"
