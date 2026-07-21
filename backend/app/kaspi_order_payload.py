from __future__ import annotations

from typing import Any


# Kaspi exposes a coarse public API lifecycle plus more precise seller-operation
# facts in Merchant Cabinet. LEO derives stages only from facts confirmed against
# real Seller GraphQL payloads:
# - terminal lifecycle statuses always win;
# - KASPI_DELIVERY_TRANSMITTED / kdTransmittedToCourier means the parcel left the seller;
# - KASPI_DELIVERY_WAIT_FOR_COURIER / kdAssembled means the parcel is packed and
#   waiting for physical handover;
# - KASPI_DELIVERY_CARGO_ASSEMBLY / isOrderArrived means the product is available
#   and the order is in packaging;
# - KASPI_DELIVERY_WAIT_FOR_POINT_DELIVERY with an unfinished PRE_ORDER step means
#   the product is still a preorder.
_STATUS_TO_IMPORT_TOKEN: dict[str, str] = {
    "NEW": "NEW",
    "SIGN_REQUIRED": "NEW",
    "APPROVED_BY_BANK": "NEW",
    "ACCEPTED": "ASSEMBLY",
    "ACCEPTED_BY_MERCHANT": "ASSEMBLY",
    "PRE_ORDERED": "ACCEPTED_BY_MERCHANT",
    "PICKUP": "ASSEMBLY",
    "ASSEMBLE": "ASSEMBLY",
    "ASSEMBLED": "HANDOVER",
    "ASSEMBLY": "ASSEMBLY",
    "HANDOVER": "HANDOVER",
    "TRANSMITTED": "SHIPPING",
    "SHIPPING": "SHIPPING",
    "HANDED_OVER_TO_COURIER": "SHIPPING",
    "DELIVERED": "DELIVERED",
    "COMPLETED": "DELIVERED",
    "ARCHIVE": "DELIVERED",
    "ARCHIVED": "DELIVERED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
    "CANCELLING": "RETURNED",
    "KASPI_DELIVERY_RETURN_REQUESTED": "RETURNED",
    "RETURNED": "RETURNED",
}

_RETURN_STATUSES = {
    "CANCELLING",
    "KASPI_DELIVERY_RETURN_REQUESTED",
    "RETURNED",
}
_CANCELLED_STATUSES = {"CANCELLED", "CANCELED"}
_DELIVERED_STATUSES = {"DELIVERED", "COMPLETED", "ARCHIVE", "ARCHIVED"}


def _has_value(attributes: dict[str, Any], *keys: str) -> bool:
    return any(attributes.get(key) not in (None, "") for key in keys)


def _nested_value(attributes: dict[str, Any], container: str, key: str) -> Any:
    nested = attributes.get(container)
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def _step_actual_time(attributes: dict[str, Any], step_name: str) -> Any:
    steps = attributes.get("orderSteps")
    if not isinstance(steps, list):
        return None
    wanted = step_name.strip().upper()
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("step") or "").strip().upper() == wanted:
            return step.get("actualTime")
    return None


def _seller_graphql_token(attributes: dict[str, Any], source_state: str) -> str | None:
    """Derive a stage from verified Merchant Cabinet GraphQL facts.

    These states were captured from real orders:
    - 1002303844: WAIT_FOR_POINT_DELIVERY -> preorder;
    - 1006563363: CARGO_ASSEMBLY -> packaging;
    - 1006480798: WAIT_FOR_COURIER -> handover;
    - 1000772384: TRANSMITTED -> shipping.
    """
    delivery = attributes.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}

    kd_transmitted = delivery.get("kdTransmittedToCourier") is True
    kd_assembled = delivery.get("kdAssembled") is True
    order_arrived = delivery.get("isOrderArrived") is True
    preorder_actual = _step_actual_time(attributes, "PRE_ORDER")
    transmission_actual = _step_actual_time(attributes, "TRANSMISSION")

    if source_state == "KASPI_DELIVERY_TRANSMITTED" or kd_transmitted or transmission_actual not in (None, ""):
        return "SHIPPING"

    if source_state == "KASPI_DELIVERY_WAIT_FOR_COURIER" or kd_assembled:
        return "HANDOVER"

    if source_state == "KASPI_DELIVERY_CARGO_ASSEMBLY" or order_arrived or preorder_actual not in (None, ""):
        return "ASSEMBLY"

    if source_state == "KASPI_DELIVERY_WAIT_FOR_POINT_DELIVERY" and attributes.get("preOrder") is True:
        return "ACCEPTED_BY_MERCHANT"

    return None


def _operational_token(attributes: dict[str, Any], source_status: str, source_state: str) -> str:
    # Terminal customer-order lifecycle statuses always win over stale
    # fulfilment flags retained by Kaspi.
    if source_status in _CANCELLED_STATUSES:
        return "CANCELLED"
    if source_status in _RETURN_STATUSES:
        return "RETURNED"
    if source_status in _DELIVERED_STATUSES or _has_value(
        attributes,
        "deliveryDate",
        "deliveredAt",
        "actualDeliveryDate",
    ):
        return "DELIVERED"

    seller_graphql_token = _seller_graphql_token(attributes, source_state)
    if seller_graphql_token is not None:
        return seller_graphql_token

    # Public Kaspi API fallback. Verified order 1000772384: an actual courier
    # transmission date means the parcel was physically transferred to logistics.
    courier_transmission_date = _nested_value(
        attributes,
        "kaspiDelivery",
        "courierTransmissionDate",
    )
    if courier_transmission_date not in (None, ""):
        return "SHIPPING"

    if source_status in {"SHIPPING", "HANDED_OVER_TO_COURIER", "TRANSMITTED"} or _has_value(
        attributes,
        "shipmentDate",
        "shippedAt",
        "actualShipmentDate",
        "handoverDate",
        "handedOverAt",
    ):
        return "SHIPPING"

    if source_status in {"ACCEPTED_BY_MERCHANT", "ACCEPTED", "PRE_ORDERED", "ASSEMBLED"}:
        if attributes.get("assembled") is True or source_status == "ASSEMBLED":
            return "HANDOVER"
        if attributes.get("preOrder") is True:
            return "ACCEPTED_BY_MERCHANT"
        return "ASSEMBLY"

    return _STATUS_TO_IMPORT_TOKEN.get(source_status, "UNKNOWN")


def canonicalize_kaspi_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an importer-facing payload without losing Kaspi source facts.

    `marketplaceState` and `marketplaceStatus` retain the exact source values.
    `status` and `state` are rewritten to one normalized importer token derived
    from confirmed seller-cabinet facts. The coarse public fulfilment bucket
    (`KASPI_DELIVERY`) never determines the lifecycle by itself.
    """
    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = dict(source_attributes) if isinstance(source_attributes, dict) else dict(payload)

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()
    import_token = _operational_token(attributes, source_status, source_state)

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["status"] = import_token
    attributes["state"] = import_token

    canonical["attributes"] = attributes
    return canonical
