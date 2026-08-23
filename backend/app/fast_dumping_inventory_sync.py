from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fast_dumping_offer_runtime as offer_runtime
from . import fast_dumping_service as svc
from .dumping_service import physical_stock_count, resolve_cost_source
from .fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from .models import Product


_INSTALLED = False
_PREVIOUS_COMPLETE_SCAN = None
_PREVIOUS_COMPLETE_APPLY = None
_SHORT_VERIFY_MARKERS = {"offer_state_pending", "waiting_existing_operation"}


def _complete_scan(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    succeeded: bool,
    market_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    before_job = db.get(FastDumpingJob, job_id)
    before_state = None
    previous_desired_stock = None
    if before_job is not None:
        before_state = db.scalar(
            select(FastDumpingState).where(
                FastDumpingState.workspace_id == workspace_id,
                FastDumpingState.product_id == before_job.product_id,
            )
        )
        if before_state is not None:
            previous_desired_stock = before_state.desired_stock_count

    result = _PREVIOUS_COMPLETE_SCAN(
        db,
        workspace_id=workspace_id,
        job_id=job_id,
        agent_id=agent_id,
        lease_token=lease_token,
        succeeded=succeeded,
        market_payload=market_payload,
        error_code=error_code,
        error_message=error_message,
    )
    if not succeeded or result.get("queued_apply"):
        return result

    job = db.get(FastDumpingJob, job_id)
    if job is None:
        return result
    state = db.scalar(
        select(FastDumpingState).where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == job.product_id,
        )
    )
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == workspace_id,
        )
    )
    product = db.scalar(
        select(Product).where(
            Product.id == job.product_id,
            Product.workspace_id == workspace_id,
        )
    )
    if state is None or policy is None or product is None:
        return result
    if previous_desired_stock is None:
        return result
    if state.active_job_id is not None:
        return result
    if not policy.enabled or not product.sale_enabled or state.automatic_writes_paused:
        return result

    stock = physical_stock_count(db, product_id=product.id)
    if int(previous_desired_stock) == int(stock):
        return result
    source = resolve_cost_source(db, product_id=product.id, inventory_first=True)
    if stock <= 0 or source is None or source.kind != "inventory" or state.own_price_kzt is None:
        return result

    decision = offer_runtime._inventory_decision(job, state, stock)
    offer_runtime._reactivate_apply(
        state=state,
        job=job,
        decision=decision,
        reason=(
            f"FIFO изменился с {int(previous_desired_stock)} до {stock}. "
            "Fast Dumping синхронизирует только realtime stock/preOrder, не трогая XML."
        ),
    )
    return {"status": state.status, "queued_apply": True, "decision": decision}


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
    if not bool(write_payload.get("accepted")) or bool(write_payload.get("verified")):
        return result
    if str(write_payload.get("error_code") or "") in _SHORT_VERIFY_MARKERS:
        return result

    # Backward compatibility for an old Agent or a direct service test: only
    # Agent 1.1+ explicitly marks Merchant-BFF pending writes. Legacy accepted
    # writes retain the configured 5/10/15/30/35/60-minute verify cadence.
    job = db.get(FastDumpingJob, job_id)
    if job is None:
        return result
    state = db.scalar(
        select(FastDumpingState).where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == job.product_id,
        )
    )
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == workspace_id,
        )
    )
    if state is None or policy is None or state.last_applied_at is None:
        return result
    legacy_verify_at = state.last_applied_at + timedelta(
        seconds=int(policy.scan_interval_seconds)
    )
    if job.not_before_at is None or job.not_before_at < legacy_verify_at:
        job.not_before_at = legacy_verify_at
    state.next_scan_at = job.not_before_at
    return result


def install_fast_dumping_inventory_sync() -> None:
    global _INSTALLED, _PREVIOUS_COMPLETE_SCAN, _PREVIOUS_COMPLETE_APPLY
    if _INSTALLED:
        return
    _INSTALLED = True

    # The first runtime version intentionally forced an offer-sync apply for all
    # benign pricing statuses. That broke cooldown/floor/delivery semantics and
    # was unnecessary. Only a real FIFO delta should force a stock-only write.
    offer_runtime.SAFE_INVENTORY_SYNC_STATUSES = set()

    _PREVIOUS_COMPLETE_SCAN = svc.complete_scan
    _PREVIOUS_COMPLETE_APPLY = svc.complete_apply
    svc.complete_scan = _complete_scan
    svc.complete_apply = _complete_apply
