from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import dumping_service
from . import fast_dumping_service as svc
from .dumping_service import physical_stock_count
from .fast_dumping_models import FastDumpingPolicy
from .models import Product


_INSTALLED = False
_PREVIOUS_SYNC = None
_PREVIOUS_PUBLISH = None


def _fast_policy(db: Session, product_id: int) -> FastDumpingPolicy | None:
    return db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.product_id == product_id,
            FastDumpingPolicy.enabled.is_(True),
        )
    )


def _sync_product_inventory_to_feed(
    db: Session,
    *,
    product_id: int,
    reason: str,
) -> dict[str, int | str | None]:
    policy = _fast_policy(db, product_id)
    if policy is None:
        return _PREVIOUS_SYNC(db, product_id=product_id, reason=reason)

    stock = physical_stock_count(db, product_id=product_id)
    state = svc.ensure_state(db, policy=policy, workspace_id=policy.workspace_id)
    state.next_scan_at = svc.utcnow()
    if state.active_job_id is None and not state.automatic_writes_paused:
        svc.queue_scan(
            db,
            policy=policy,
            workspace_id=policy.workspace_id,
            reason=f"inventory_event:{reason}"[:128],
        )
    return {
        "stock_count": stock,
        "xml_state": "fast_realtime_owned",
        "supplier_job_id": None,
    }


def _publish_decision(db: Session, *, product: Product, policy: Any, decision: Any):
    if _fast_policy(db, product.id) is None:
        return _PREVIOUS_PUBLISH(db, product=product, policy=policy, decision=decision)

    original_auto_publish = bool(policy.auto_publish_xml)
    policy.auto_publish_xml = False
    try:
        run = _PREVIOUS_PUBLISH(db, product=product, policy=policy, decision=decision)
    finally:
        policy.auto_publish_xml = original_auto_publish
    run.published = False
    run.explanation_json = {
        **(run.explanation_json or {}),
        "xml_publication": "suppressed_fast_realtime_owner",
        "offer_owner": "fast_realtime",
    }
    return run


def install_fast_dumping_xml_guard() -> None:
    global _INSTALLED, _PREVIOUS_SYNC, _PREVIOUS_PUBLISH
    if _INSTALLED:
        return
    _INSTALLED = True
    _PREVIOUS_SYNC = dumping_service.sync_product_inventory_to_feed
    _PREVIOUS_PUBLISH = dumping_service.publish_decision
    dumping_service.sync_product_inventory_to_feed = _sync_product_inventory_to_feed
    dumping_service.publish_decision = _publish_decision
