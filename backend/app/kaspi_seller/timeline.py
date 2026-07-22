from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .snapshot_models import KaspiSellerOrderSnapshotRecord
from .timeline_models import KaspiSellerOrderTimelineEvent


_STAGE_EVENT_TYPES = {
    "ACCEPTED_BY_MERCHANT": "ORDER_ACCEPTED",
    "ASSEMBLY": "ORDER_ASSEMBLY_STARTED",
    "HANDOVER": "ORDER_ASSEMBLED",
    "SHIPPING": "ORDER_TRANSFERRED",
    "DELIVERED": "ORDER_DELIVERED",
    "RETURNED": "ORDER_RETURNED",
    "CANCELLED": "ORDER_CANCELLED",
}


@dataclass(frozen=True, slots=True)
class TimelineTransition:
    event_type: str
    from_stage: str | None
    to_stage: str | None
    payload: dict[str, Any]


def derive_timeline_transition(
    *,
    previous_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
) -> TimelineTransition | None:
    current_stage = _text(current_snapshot.get("stage"))
    previous_stage = _text(previous_snapshot.get("stage")) if previous_snapshot else None

    if previous_snapshot is not None and previous_stage == current_stage:
        return None

    event_type = _STAGE_EVENT_TYPES.get(current_stage or "", "ORDER_STAGE_CHANGED")
    return TimelineTransition(
        event_type=event_type,
        from_stage=previous_stage,
        to_stage=current_stage,
        payload={
            "from_stage": previous_stage,
            "to_stage": current_stage,
            "state": _text(current_snapshot.get("state")),
            "status": _text(current_snapshot.get("status")),
            "preorder": current_snapshot.get("preorder") is True,
        },
    )


def persist_timeline_for_snapshot(
    db: Session,
    *,
    snapshot_id: int,
) -> tuple[int, ...]:
    current = db.get(KaspiSellerOrderSnapshotRecord, snapshot_id)
    if current is None:
        raise ValueError(f"Kaspi Seller snapshot {snapshot_id} was not found")
    if not current.changed:
        return ()

    existing_ids = tuple(
        db.scalars(
            select(KaspiSellerOrderTimelineEvent.id).where(
                KaspiSellerOrderTimelineEvent.snapshot_id == snapshot_id
            )
        ).all()
    )
    if existing_ids:
        return existing_ids

    previous = (
        db.get(KaspiSellerOrderSnapshotRecord, current.previous_snapshot_id)
        if current.previous_snapshot_id is not None
        else None
    )
    current_payload = json.loads(current.snapshot_payload)
    previous_payload = json.loads(previous.snapshot_payload) if previous is not None else None
    transition = derive_timeline_transition(
        previous_snapshot=previous_payload,
        current_snapshot=current_payload,
    )
    if transition is None:
        return ()

    event = KaspiSellerOrderTimelineEvent(
        snapshot_id=current.id,
        previous_snapshot_id=previous.id if previous is not None else None,
        merchant_id=current.merchant_id,
        order_code=current.order_code,
        event_type=transition.event_type,
        from_stage=transition.from_stage,
        to_stage=transition.to_stage,
        event_payload=json.dumps(
            transition.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        occurred_at=current.observed_at,
    )
    db.add(event)
    db.flush()
    return (event.id,)


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None
