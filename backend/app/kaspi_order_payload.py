from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any

from .kaspi_order_board import classify_kaspi_order_details


_IMPORT_TOKEN_BY_STAGE = {
    "preorder": "ACCEPTED_BY_MERCHANT",
    "assembly": "ASSEMBLY",
    "handover": "HANDOVER",
    "shipping": "SHIPPING",
    "cancelling": "CANCELLING",
    "cancelled": "CANCELLED",
    "delivered": "DELIVERED",
    "returned": "RETURNED",
    "unknown": "UNKNOWN",
}


def canonicalize_kaspi_product_id(value: Any) -> str | None:
    """Return Kaspi's stable numeric product id when JSON:API encodes it.

    Newly created order entries can expose a relationship id such as
    ``MTA1NTc5OTQx`` while the seller XML and product registry use ``105579941``.
    Kaspi later returns the decoded id from the product endpoint, which used to
    leave the order as ``Unknown product`` until the slower enrichment cycle.

    Decode only strict Base64 values whose ASCII payload is a plausible numeric
    Kaspi id. Opaque JSON:API ids remain byte-for-byte unchanged.
    """

    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.isdigit():
        return raw or None
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        decoded = base64.b64decode(padded, validate=True).decode("ascii").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw
    if decoded.isdigit() and 6 <= len(decoded) <= 18:
        return decoded
    return raw


def canonicalize_kaspi_order_payload(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    history_record: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Almaty",
    handoff_cutoff_hour: int = 21,
) -> dict[str, Any]:
    """Map archive board output into the existing Commerce Core model."""

    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = (
        dict(source_attributes)
        if isinstance(source_attributes, dict)
        else dict(payload)
    )

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()
    classification = classify_kaspi_order_details(
        attributes,
        timezone_name=timezone_name,
        now=now,
        handoff_cutoff_hour=handoff_cutoff_hour,
        history_record=history_record,
    )
    board_stage = classification.stage
    import_token = _IMPORT_TOKEN_BY_STAGE[board_stage]

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["leoOrderStage"] = board_stage
    attributes["leoOrderType"] = classification.order_type
    attributes["leoClassificationSource"] = classification.source
    attributes["status"] = import_token
    attributes["state"] = import_token

    canonical["attributes"] = attributes
    return canonical
