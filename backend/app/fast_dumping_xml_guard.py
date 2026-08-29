from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import dumping_service
from . import fast_dumping_service as svc
from .dumping_service import physical_stock_count
from .fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from .models import Product


_INSTALLED = False
_PREVIOUS_SYNC = None
_PREVIOUS_PUBLISH = None
_PREVIOUS_COMPLETE_APPLY = None
_PREVIOUS_COMPLETE_VERIFICATION = None


_QUEUED_REPRICE_STATUSES = {"queued_apply"}


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
    state.inventory_on_hand = stock
    state.next_scan_at = svc.utcnow()

    # An inventory event is authoritative for the fulfillment mode. A queued
    # apply may still contain the supplier-preorder decision produced before a
    # physical batch was received (or the inverse after FIFO exhaustion). It is
    # safe to replace such an unleased apply with a fresh scan. Leased writes
    # and queued verification are deliberately left single-flight: their
    # completion path will schedule the already-due rescan without risking a
    # second mutation while Kaspi is processing the first one.
    active = (
        db.get(FastDumpingJob, state.active_job_id)
        if state.active_job_id
        else None
    )
    if active is not None and active.status in _QUEUED_REPRICE_STATUSES:
        svc.cancel_active_job(
            db,
            state=state,
            reason=(
                f"FIFO-остаток изменился до {stock}; старое offer-state решение "
                "заменено обязательным новым scan."
            ),
        )
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


def _mirror_verified_offer(db: Session, *, job: FastDumpingJob) -> bool:
    """Mirror a verified Fast offer into generated XML as a rollback guard.

    Fast realtime remains the owner. XML is only kept consistent so a scheduled
    Kaspi catalog pull cannot resurrect an older price/stock/preOrder state.
    """

    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == job.workspace_id,
        )
    )
    product = db.scalar(
        select(Product).where(
            Product.id == job.product_id,
            Product.workspace_id == job.workspace_id,
        )
    )
    if policy is None or product is None or not policy.enabled:
        return False

    feed = dumping_service._latest_feed_for_update(db)
    if feed is None:
        return False

    decision = dict(job.decision_json or {})
    mode = str(decision.get("fulfillment_mode") or "inventory")
    stock = int(decision.get("stock_count") or 0)
    preorder = int(decision.get("preorder_days") or 0)
    current_xml = feed.generated_xml or feed.source_xml
    sku_candidates = dumping_service._sku_candidates(product)

    try:
        if mode == "off":
            mirrored = dumping_service.set_feed_offer_availability(
                current_xml,
                sku_candidates=sku_candidates,
                available=False,
                stock_count=0,
                preorder_days=0,
            )
        else:
            target_raw = decision.get("target_price_kzt")
            if target_raw in (None, ""):
                state = db.scalar(
                    select(FastDumpingState).where(
                        FastDumpingState.workspace_id == job.workspace_id,
                        FastDumpingState.product_id == job.product_id,
                    )
                )
                target_raw = (
                    state.own_price_kzt
                    if state is not None and state.own_price_kzt is not None
                    else None
                )
            if target_raw in (None, ""):
                return False
            mirrored = dumping_service.update_feed_xml(
                current_xml,
                sku_candidates=sku_candidates,
                price_kzt=Decimal(str(target_raw)),
                preorder_days=preorder,
                stock_count=stock,
                product=product,
                city_id=policy.city_id,
            )
        feed.generated_xml = mirrored
        feed.active = True
        feed.generated_at = func.now()
        return True
    except ValueError as exc:
        # If the SKU is absent from XML there is nothing stale for an hourly
        # XML pull to overwrite. Realtime ownership can continue safely.
        if str(exc) == "Товар не найден в сохранённом XML по SKU/Kaspi ID":
            return False
        raise


def _complete_apply(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    write_payload: dict[str, Any],
) -> dict[str, Any]:
    result = _PREVIOUS_COMPLETE_APPLY(
        db,
        workspace_id=workspace_id,
        job_id=job_id,
        agent_id=agent_id,
        lease_token=lease_token,
        write_payload=write_payload,
    )
    if bool(write_payload.get("accepted")) and bool(write_payload.get("verified")):
        job = db.get(FastDumpingJob, job_id)
        if job is not None and _mirror_verified_offer(db, job=job):
            state = db.scalar(
                select(FastDumpingState).where(
                    FastDumpingState.workspace_id == workspace_id,
                    FastDumpingState.product_id == job.product_id,
                )
            )
            if state is not None:
                state.status_reason = (
                    f"{state.status_reason or ''} XML safety mirror обновлён после realtime verify."
                ).strip()
    return result


def _complete_verification(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    observed_own_price_kzt: object,
    verification_succeeded: bool = True,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    result = _PREVIOUS_COMPLETE_VERIFICATION(
        db,
        workspace_id=workspace_id,
        job_id=job_id,
        agent_id=agent_id,
        lease_token=lease_token,
        observed_own_price_kzt=observed_own_price_kzt,
        verification_succeeded=verification_succeeded,
        error_code=error_code,
        error_message=error_message,
    )
    if verification_succeeded:
        job = db.get(FastDumpingJob, job_id)
        if job is not None and job.status == "succeeded" and _mirror_verified_offer(db, job=job):
            state = db.scalar(
                select(FastDumpingState).where(
                    FastDumpingState.workspace_id == workspace_id,
                    FastDumpingState.product_id == job.product_id,
                )
            )
            if state is not None:
                state.status_reason = (
                    f"{state.status_reason or ''} XML safety mirror синхронизирован с подтверждённым Kaspi state."
                ).strip()
    return result


def install_fast_dumping_xml_guard() -> None:
    global _INSTALLED, _PREVIOUS_SYNC, _PREVIOUS_PUBLISH
    global _PREVIOUS_COMPLETE_APPLY, _PREVIOUS_COMPLETE_VERIFICATION
    if _INSTALLED:
        return
    _INSTALLED = True
    _PREVIOUS_SYNC = dumping_service.sync_product_inventory_to_feed
    _PREVIOUS_PUBLISH = dumping_service.publish_decision
    _PREVIOUS_COMPLETE_APPLY = svc.complete_apply
    _PREVIOUS_COMPLETE_VERIFICATION = svc.complete_verification
    dumping_service.sync_product_inventory_to_feed = _sync_product_inventory_to_feed
    dumping_service.publish_decision = _publish_decision
    svc.complete_apply = _complete_apply
    svc.complete_verification = _complete_verification
