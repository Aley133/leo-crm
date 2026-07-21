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


def test_preorder_is_detected_from_explicit_preorder_flag() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=True,
        assembled=False,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784642859000,
        },
    )

    canonical = canonicalize_kaspi_order_payload(payload)

    assert canonical["attributes"]["marketplaceStatus"] == "ACCEPTED_BY_MERCHANT"
    assert _normalized(payload) == "accepted"
    assert _order(status="accepted", line=_line()).stage == CommerceOrderStage.PREORDER


def test_preorder_flag_wins_over_technical_transmission_date() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=True,
        assembled=True,
        kaspiDelivery={
            "courierTransmissionDate": 1784642859000,
            "courierTransmissionPlanningDate": 1784642859000,
        },
    )

    assert _normalized(payload) == "accepted"
    assert _order(status="accepted", line=_line()).stage == CommerceOrderStage.PREORDER


def test_normal_merchant_acceptance_is_packaging() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        assembled=False,
        kaspiDelivery={"courierTransmissionDate": None},
    )

    assert _normalized(payload) == "assembly"
    assert _order(status="assembly", line=_line()).stage == CommerceOrderStage.ASSEMBLY


def test_assembled_flag_alone_does_not_promote_packaging_to_handover() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
        assembled=True,
        kaspiDelivery={
            "courierTransmissionDate": None,
            "courierTransmissionPlanningDate": 1784642859000,
        },
    )

    assert _normalized(payload) == "assembly"
    assert _order(status="assembly", line=_line()).stage == CommerceOrderStage.ASSEMBLY


def test_actual_courier_transmission_after_preorder_means_shipping() -> None:
    payload = _payload(
        status="ACCEPTED_BY_MERCHANT",
        preOrder=False,
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


def test_procurement_state_does_not_rewrite_customer_order_stage() -> None:
    preorder_with_received_purchase = _order(
        status="accepted",
        line=_line(purchase_request_id="pr-1", purchase_status="received"),
    )
    assembly_with_requested_purchase = _order(
        status="assembly",
        line=_line(purchase_request_id="pr-2", purchase_status="requested"),
    )

    assert preorder_with_received_purchase.stage == CommerceOrderStage.PREORDER
    assert assembly_with_requested_purchase.stage == CommerceOrderStage.ASSEMBLY
