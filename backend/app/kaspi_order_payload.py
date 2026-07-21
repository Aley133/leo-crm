from __future__ import annotations

from typing import Any

from .kaspi_seller import map_seller_order_facts, resolve_seller_stage


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

    # Precise Merchant Cabinet lifecycle. This resolver consumes either raw
    # Seller GraphQL orderDetail fields or equivalent importer attributes.
    seller_facts = map_seller_order_facts(attributes)
    seller_token = resolve_seller_stage(seller_facts)
    if seller_token is not None:
        return seller_token

    # Public Kaspi API fallback. An actual courier transmission date means the
    # parcel was physically transferred to logistics.
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
    `status` and `state` are rewritten to one normalized importer token. Precise
    Seller GraphQL facts are authoritative when present; the public API remains
    a conservative fallback for orders not yet enriched by Browser Agent.
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
