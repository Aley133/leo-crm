from datetime import UTC, datetime
from decimal import Decimal

from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine, CommerceOrderStage
from backend.app.kaspi_order_payload import canonicalize_kaspi_order_payload
from backend.app.marketplace_import import normalize_kaspi_order


def _payload(*, state: str, status: str) -> dict:
    return {
        "id": "order-1",
        "attributes": {
            "code": "1000025629",
            "state": state,
            "status": status,
            "creationDate": "2026-07-18T10:00:00Z",
            "totalPrice": 9999,
            "entries": [],
        },
    }


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


def test_kaspi_status_wins_over_delivery_state() -> None:
    canonical = canonicalize_kaspi_order_payload(
        _payload(state="KASPI_DELIVERY", status="CANCELLED")
    )
    normalized = normalize_kaspi_order(canonical)

    assert canonical["attributes"]["marketplaceState"] == "KASPI_DELIVERY"
    assert canonical["attributes"]["marketplaceStatus"] == "CANCELLED"
    assert normalized.status == "cancelled"


def test_completed_order_is_delivered_even_in_kaspi_delivery_channel() -> None:
    canonical = canonicalize_kaspi_order_payload(
        _payload(state="KASPI_DELIVERY", status="COMPLETED")
    )
    assert normalize_kaspi_order(canonical).status == "delivered"


def test_return_request_is_not_misclassified_as_delivery() -> None:
    canonical = canonicalize_kaspi_order_payload(
        _payload(
            state="KASPI_DELIVERY",
            status="KASPI_DELIVERY_RETURN_REQUESTED",
        )
    )
    assert normalize_kaspi_order(canonical).status == "returned"


def test_accepted_order_without_inventory_fact_remains_accepted() -> None:
    order = _order(status="accepted", line=_line())
    assert order.stage == CommerceOrderStage.ACCEPTED


def test_accepted_order_moves_to_preorder_only_after_purchase_exists() -> None:
    order = _order(
        status="accepted",
        line=_line(purchase_request_id="pr-1", purchase_status="ordered"),
    )
    assert order.stage == CommerceOrderStage.PREORDER


def test_accepted_order_moves_to_assembly_after_receipt() -> None:
    order = _order(
        status="accepted",
        line=_line(purchase_request_id="pr-1", purchase_status="received"),
    )
    assert order.stage == CommerceOrderStage.ASSEMBLY
