from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MarketplaceAccount
from .schema_guard import ensure_kaspi_seller_storage_schema
from .snapshot_models import KaspiSellerOrderSnapshotRecord
from .timeline import persist_timeline_for_snapshot


class KaspiSellerSnapshotError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedKaspiSellerSnapshot:
    snapshot_id: int
    changed: bool
    previous_snapshot_id: int | None
    timeline_event_ids: tuple[int, ...] = ()


def persist_kaspi_seller_snapshot(
    db: Session,
    *,
    browser_agent_job_id: int,
    payload: dict[str, Any],
    observed_at: datetime,
) -> PersistedKaspiSellerSnapshot:
    """Append one immutable observation and derive its business timeline event."""

    # Production may contain a database stamped past the migration while the
    # nullable account link column is physically absent. Repair that drift before
    # any ORM entity query, otherwise SQLAlchemy selects the missing column and
    # the Browser Agent completion request fails with HTTP 500.
    ensure_kaspi_seller_storage_schema(db)

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise KaspiSellerSnapshotError("Kaspi Seller result requires normalized snapshot")

    merchant_id = _required_text(snapshot.get("merchant_id") or payload.get("merchant_id"), "merchant_id")
    order_code = _required_text(snapshot.get("order_code") or payload.get("order_code"), "order_code")
    state = _required_text(snapshot.get("state"), "state")
    status = _required_text(snapshot.get("status"), "status")
    stage = _optional_text(snapshot.get("stage"))
    schema_version = _optional_text(snapshot.get("schema_version") or payload.get("schema_version"))

    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    existing_for_job = db.scalar(
        select(KaspiSellerOrderSnapshotRecord).where(
            KaspiSellerOrderSnapshotRecord.browser_agent_job_id == browser_agent_job_id
        )
    )
    if existing_for_job is not None:
        event_ids = persist_timeline_for_snapshot(db, snapshot_id=existing_for_job.id)
        return PersistedKaspiSellerSnapshot(
            snapshot_id=existing_for_job.id,
            changed=existing_for_job.changed,
            previous_snapshot_id=existing_for_job.previous_snapshot_id,
            timeline_event_ids=event_ids,
        )

    previous = db.scalar(
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
    account = db.scalar(
        select(MarketplaceAccount).where(
            MarketplaceAccount.provider == "kaspi",
            MarketplaceAccount.external_account_id == merchant_id,
        )
    )
    changed = previous is None or previous.snapshot_fingerprint != fingerprint

    record = KaspiSellerOrderSnapshotRecord(
        browser_agent_job_id=browser_agent_job_id,
        marketplace_account_id=account.id if account is not None else None,
        previous_snapshot_id=previous.id if previous is not None else None,
        merchant_id=merchant_id,
        order_code=order_code,
        state=state,
        status=status,
        stage=stage,
        schema_version=schema_version,
        snapshot_fingerprint=fingerprint,
        changed=changed,
        snapshot_payload=serialized,
        observed_at=observed_at,
    )
    db.add(record)
    db.flush()
    event_ids = persist_timeline_for_snapshot(db, snapshot_id=record.id)
    return PersistedKaspiSellerSnapshot(
        snapshot_id=record.id,
        changed=record.changed,
        previous_snapshot_id=record.previous_snapshot_id,
        timeline_event_ids=event_ids,
    )


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise KaspiSellerSnapshotError(f"Kaspi Seller snapshot requires {field_name}")
    return text


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None
