from __future__ import annotations

from datetime import datetime
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


def canonicalize_kaspi_order_payload(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    history_record: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Almaty",
    handoff_cutoff_hour: int = 21,
) -> dict[str, Any]:
    """Normalize one official Kaspi Orders API payload for Commerce Core.

    Operational stage is produced exclusively by the raw receiver model. Exact
    Kaspi state/status are retained, while transition history can distinguish
    handover from transmitted orders when Kaspi omits transmission timestamps.
    """

    canonical = dict(payload)
    source_attributes = payload.get("attributes")
    attributes = dict(source_attributes) if isinstance(source_attributes, dict) else dict(payload)

    source_state = str(attributes.get("state") or "UNKNOWN").strip().upper()
    source_status = str(attributes.get("status") or "UNKNOWN").strip().upper()
    board_stage = classify_kaspi_order(
        attributes,
        timezone_name=timezone_name,
        now=now,
        handoff_cutoff_hour=handoff_cutoff_hour,
        history_record=history_record,
    )
    import_token = _IMPORT_TOKEN_BY_STAGE[board_stage]

    attributes["marketplaceState"] = source_state
    attributes["marketplaceStatus"] = source_status
    attributes["leoOrderStage"] = board_stage
    attributes["status"] = import_token
    attributes["state"] = import_token

    canonical["attributes"] = attributes
    return canonical
