from __future__ import annotations

from typing import Any


# Kaspi exposes a coarse lifecycle status plus seller-operation facts. LEO CRM
# derives the visible operational stage only from facts confirmed by the seller
# cabinet/API:
# - terminal lifecycle statuses (cancelled, returned, completed) are authoritative;
# - a non-null kaspiDelivery.courierTransmissionDate means the parcel was
#   physically handed to Kaspi logistics;
# - preOrder=true means the seller is still waiting for the preorder item;
# - assembled=true with no actual courier transmission means the parcel is ready
#   for handover;
# - normal merchant acceptance means packing/assembly.
_STATUS_TO_IMPORT_TOKEN: dict[str, str] = {
    "NEW": "NEW",
    "SIGN_REQUIRED": "NEW",
    "APPROVED_BY_BANK": "NEW",
    "ACCEPTED_BY_MERCHANT": "ASSEMBLY",
    "PICKUP": "ASSEMBLY",
    "ASSEMBLE": "ASSEMBLY",
    "ASSEMBLY": "ASSEMBLY",
    "HANDOVER": "HANDOVER",
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


def _operational_token(attributes: dict[str, Any], source_status: str) -> str:
    # Terminal customer-order lifecycle statuses always win over operational
    # flags left on the payload from earlier stages.
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

    # This is the exact seller-cabinet fact for "Передан курьеру". It must be
    # evaluated before preOrder/assembled because Kaspi may keep both flags true
    # after the parcel has already left the seller.
    courier_transmission_date = _nested_value(
        attributes,
        "kaspiDelivery",
        "courierTransmissionDate",
    )
    if courier_transmission_date not in (None, ""):
        return "SHIPPING"

    # Keep support for explicit/legacy shipment fields returned by other Kaspi
    # payload variants, but never treat a planning date as an actual handover.
    if source_status in {"SHIPPING", "HANDED_OVER_TO_COURIER"} or _has_value(
        attributes,
        "shipmentDate",
        "shippedAt",
        "actualShipmentDate",
        "handoverDate",
        "handedOverAt",
    ):
        return "SHIPPING"

    if source_status == "ACCEPTED_BY_MERCHANT":
        if attributes.get("preOrder") is True:
            return "ACCEPTED_BY_MERCHANT"
        if attributes.get("assembled") is True:
            return "HANDOVER"
        return "ASSEMBLY"

    return _STATUS_TO_IMPORT_TOKEN.get(source_status, "UNKNOWN")


def canonicalize_kaspi_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an importer-facing payload without losing Kaspi source facts.

    `marketplaceState` and `marketplaceStatus` retain the exact source values.
    `status` and `state` are rewritten to one normalized importer token derived
    from confirmed seller-cabinet facts. The fulfilment bucket
    (`KASPI_DELIVERY`) never determines the lifecycle by itself.
    """
    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = dict(source_attributes) if isinstance(source_attributes, dict) else dict(payload)

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()
    import_token = _operational_token(attributes, source_status)

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["status"] = import_token
    attributes["state"] = import_token

    canonical["attributes"] = attributes
    return canonical
