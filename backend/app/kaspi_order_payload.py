from __future__ import annotations

from typing import Any


# Kaspi exposes a coarse lifecycle status plus seller-operation facts. LEO CRM
# derives only stages that can be proven from the live seller payload:
# - terminal lifecycle statuses are authoritative;
# - actual courierTransmissionDate means the order left the seller;
# - assembled=true without actual transmission means the parcel is packed and
#   waiting for handover;
# - preOrder=true without assembly/transmission means the order still requires a
#   separate stock decision in Commerce Core.
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

    # Verified order 1000772384: this is the authoritative fact that the parcel
    # was physically transferred to Kaspi logistics. It wins over stale
    # preOrder/assembled flags.
    courier_transmission_date = _nested_value(
        attributes,
        "kaspiDelivery",
        "courierTransmissionDate",
    )
    if courier_transmission_date not in (None, ""):
        return "SHIPPING"

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
        # Verified order 1006480798: packed, waybill created, but not yet
        # physically transferred to Kaspi logistics.
        if attributes.get("assembled") is True:
            return "HANDOVER"

        # Kaspi alone cannot distinguish preorder 1002303844 from packaging
        # 1006563363: both expose preOrder=true, assembled=false and no actual
        # courier transmission. Commerce Core must resolve that boundary using
        # warehouse availability/reservation facts.
        if attributes.get("preOrder") is True:
            return "ACCEPTED_BY_MERCHANT"

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
