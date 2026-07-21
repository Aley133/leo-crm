from __future__ import annotations

from typing import Any


# Kaspi exposes a coarse lifecycle status plus fulfilment dates. The seller
# cabinet uses those facts to distinguish the operational stages that matter to
# LEO CRM:
# - planned arrival/reservation date => preorder is still on the way to seller;
# - accepted by merchant without arrival date => order is ready for packing;
# - actual shipment/handover date => order was handed to Kaspi logistics;
# - cancellation/return/completion statuses remain authoritative.
_STATUS_TO_IMPORT_TOKEN: dict[str, str] = {
    "NEW": "NEW",
    "SIGN_REQUIRED": "NEW",
    "APPROVED_BY_BANK": "NEW",
    "ACCEPTED_BY_MERCHANT": "ASSEMBLY",
    "PICKUP": "ASSEMBLY",
    "ASSEMBLE": "ASSEMBLY",
    "ASSEMBLY": "ASSEMBLY",
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


def _operational_token(attributes: dict[str, Any], source_status: str) -> str:
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

    # Actual handover/shipment is the only safe signal that the order has left
    # the seller. plannedShipmentDate is deliberately excluded: it is merely a
    # deadline and exists before the parcel is handed over.
    if source_status in {"SHIPPING", "HANDED_OVER_TO_COURIER"} or _has_value(
        attributes,
        "shipmentDate",
        "shippedAt",
        "actualShipmentDate",
        "handoverDate",
        "handedOverAt",
    ):
        return "SHIPPING"

    # Kaspi preorder cards expose a planned arrival/reservation date and an
    # editable arrival control. These facts distinguish "В пути" from normal
    # merchant acceptance/packing.
    if source_status in {"IN_TRANSIT", "ON_THE_WAY"} or _has_value(
        attributes,
        "reservationDate",
        "plannedArrivalDate",
        "plannedArrivalAt",
        "arrivalDate",
    ):
        return "ACCEPTED_BY_MERCHANT"

    return _STATUS_TO_IMPORT_TOKEN.get(source_status, "UNKNOWN")


def canonicalize_kaspi_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an importer-facing payload without losing Kaspi source facts.

    `marketplaceState` and `marketplaceStatus` retain the exact source values.
    `status` and `state` are rewritten to one normalized importer token derived
    from the actual seller-cabinet facts. The fulfilment bucket
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
