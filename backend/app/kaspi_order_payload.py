from __future__ import annotations

from typing import Any


# Kaspi documents two independent attributes:
# - state: order channel/bucket (NEW, KASPI_DELIVERY, ARCHIVE, ...)
# - status: lifecycle status (APPROVED_BY_BANK, ACCEPTED_BY_MERCHANT,
#   COMPLETED, CANCELLED, CANCELLING, KASPI_DELIVERY_RETURN_REQUESTED,
#   RETURNED).
#
# The legacy importer historically reads `state` first. Until that importer is
# migrated to a richer source-state model, this boundary adapter produces its
# canonical input while preserving the original Kaspi values for audit.
_STATUS_TO_IMPORT_TOKEN: dict[str, str] = {
    "APPROVED_BY_BANK": "NEW",
    "ACCEPTED_BY_MERCHANT": "ACCEPTED_BY_MERCHANT",
    "ASSEMBLE": "ASSEMBLY",
    "ASSEMBLY": "ASSEMBLY",
    "COMPLETED": "DELIVERED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
    "CANCELLING": "RETURNED",
    "KASPI_DELIVERY_RETURN_REQUESTED": "RETURNED",
    "RETURNED": "RETURNED",
}


def canonicalize_kaspi_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return an importer-facing payload without losing Kaspi source facts.

    `marketplaceState` and `marketplaceStatus` retain the exact source values.
    The canonical `state` contains a lifecycle token understood by the current
    importer. Unknown statuses intentionally become UNKNOWN instead of being
    guessed from the delivery channel.
    """

    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = dict(source_attributes) if isinstance(source_attributes, dict) else dict(payload)

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["state"] = _STATUS_TO_IMPORT_TOKEN.get(source_status, "UNKNOWN")

    canonical["attributes"] = attributes
    return canonical
