from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_service_token
from ..db import get_db
from .snapshot_models import KaspiSellerOrderSnapshotRecord
from .timeline_models import KaspiSellerOrderTimelineEvent


router = APIRouter(
    prefix="/api/kaspi-seller/orders",
    tags=["kaspi-seller-orders"],
    dependencies=[Depends(require_service_token)],
)


def timeline_event_payload(event: KaspiSellerOrderTimelineEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "snapshot_id": event.snapshot_id,
        "previous_snapshot_id": event.previous_snapshot_id,
        "event_type": event.event_type,
        "from_stage": event.from_stage,
        "to_stage": event.to_stage,
        "details": json.loads(event.event_payload),
        "occurred_at": event.occurred_at,
    }


def snapshot_record_payload(snapshot: KaspiSellerOrderSnapshotRecord) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "browser_agent_job_id": snapshot.browser_agent_job_id,
        "previous_snapshot_id": snapshot.previous_snapshot_id,
        "merchant_id": snapshot.merchant_id,
        "order_code": snapshot.order_code,
        "state": snapshot.state,
        "status": snapshot.status,
        "stage": snapshot.stage,
        "changed": snapshot.changed,
        "observed_at": snapshot.observed_at,
        "snapshot": json.loads(snapshot.snapshot_payload),
    }


@router.get("/{order_code}/timeline")
def read_order_timeline(
    order_code: str,
    merchant_id: str = Query(min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    events = db.scalars(
        select(KaspiSellerOrderTimelineEvent)
        .where(
            KaspiSellerOrderTimelineEvent.merchant_id == merchant_id,
            KaspiSellerOrderTimelineEvent.order_code == order_code,
        )
        .order_by(
            KaspiSellerOrderTimelineEvent.occurred_at.asc(),
            KaspiSellerOrderTimelineEvent.id.asc(),
        )
        .limit(limit)
    ).all()
    return {
        "merchant_id": merchant_id,
        "order_code": order_code,
        "count": len(events),
        "events": [timeline_event_payload(event) for event in events],
    }


@router.get("/{order_code}/latest")
def read_latest_order_snapshot(
    order_code: str,
    merchant_id: str = Query(min_length=1, max_length=128),
    db: Session = Depends(get_db),
):
    snapshot = db.scalar(
        select(KaspiSellerOrderSnapshotRecord)
        .where(
            KaspiSellerOrderSnapshotRecord.merchant_id == merchant_id,
            KaspiSellerOrderSnapshotRecord.order_code == order_code,
        )
        .order_by(
            KaspiSellerOrderSnapshotRecord.observed_at.desc(),
            KaspiSellerOrderSnapshotRecord.id.desc(),
        )
        .limit(1)
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Kaspi Seller order snapshot not found")
    return snapshot_record_payload(snapshot)
