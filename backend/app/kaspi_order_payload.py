from __future__ import annotations

from typing import Any

from .kaspi_order_board import classify_kaspi_order


_IMPORT_TOKEN_BY_STAGE = {
    "preorder": "ACCEPTED_BY_MERCHANT",
    "assembly": "ASSEMBLY",
    "handover": "HANDOVER",
    "shipping": "SHIPPING",
    "cancelled": "CANCELLED",
    "delivered": "DELIVERED",
    "returned": "RETURNED",
    "unknown": "UNKNOWN",
}


def canonicalize_kaspi_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize one official Kaspi Orders API payload for Commerce Core.

    The operational stage is produced by the raw receiver model. Browser Agent,
    Seller GraphQL, Snapshot and Decision Engine are intentionally not involved.
    Exact source values remain available as marketplaceState/marketplaceStatus.
    """

    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = dict(source_attributes) if isinstance(source_attributes, dict) else dict(payload)

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()
    board_stage = classify_kaspi_order(attributes)
    import_token = _IMPORT_TOKEN_BY_STAGE[board_stage]

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["leoOrderStage"] = board_stage
    attributes["status"] = import_token
    attributes["state"] = import_token

    canonical["attributes"] = attributes
    return canonical
