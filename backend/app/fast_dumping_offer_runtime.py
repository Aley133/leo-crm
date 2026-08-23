from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import dumping_service as classic_dumping
from . import fast_dumping_service as svc
from .dumping_service import (
    SUPPLIER_PREORDER_STOCK_COUNT,
    calculate_safe_floor,
    physical_stock_count,
    resolve_cost_source,
)
from .fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from .models import Product


OFFER_VERIFY_DELAY_SECONDS = 15
OFFER_VERIFY_MAX_ATTEMPTS = 80
SAFE_INVENTORY_SYNC_STATUSES = {
    "watching",
    "floor_limited",
    "delivery_advantage",
    "cooldown",
    "floor_only",
    "no_competitor",
}

_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}
_CLASSIC_ORIGINALS: dict[str, Any] = {}


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _clamp_preorder(value: object) -> int:
    try:
        days = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        days = 0
    return max(0, min(60, days))


def _reactivate_apply(
    *,
    state: FastDumpingState,
    job: FastDumpingJob,
    decision: dict[str, Any],
    reason: str,
) -> None:
    job.status = "queued_apply"
    job.completed_at = None
    job.agent_id = None
    job.lease_until = None
    job.lease_token = None
    job.not_before_at = None
    job.error_code = None
    job.error_message = None
    job.decision_json = decision
    job.state_version = state.state_version
    state.active_job_id = job.id
    state.status = "queued_apply"
    state.status_reason = reason
    state.next_scan_at = None
    state.last_error_code = None
    state.last_error_message = None


def _inventory_decision(job: FastDumpingJob, state: FastDumpingState, stock: int) -> dict[str, Any]:
    decision = dict(job.decision_json or {})
    target = decision.get("target_price_kzt")
    if target in (None, ""):
        target = _money(state.own_price_kzt or state.target_price_kzt)
    decision.update(
        {
            "fulfillment_mode": "inventory",
            "source_kind": "inventory",
            "stock_count": int(stock),
            "preorder_days": 0,
            "target_price_kzt": target,
        }
    )
    return decision


def _supplier_decision(
    *,
    state: FastDumpingState,
    policy: FastDumpingPolicy,
    source: Any,
) -> dict[str, Any]:
    floor = calculate_safe_floor(
        unit_cost_kzt=source.unit_cost_kzt,
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    competitor = state.competitor_price_kzt
    if competitor is None:
        target = floor
    else:
        target = max(
            floor,
            max(Decimal("1"), Decimal(competitor) - Decimal(policy.undercut_step_kzt)),
        )
    own = state.own_price_kzt
    if own is not None and not policy.allow_price_raise and target > own:
        target = max(floor, own)
    preorder = max(1, _clamp_preorder(source.delivery_days))
    return {
        "safe_floor_kzt": _money(floor),
        "competitor_price_kzt": _money(competitor),
        "own_price_kzt": _money(own),
        "target_price_kzt": _money(target),
        "status": "preorder_ready",
        "reason": (
            f"FIFO закончился. Fast Dumping переключает SKU на предзаказ поставщика "
            f"{source.name}: {preorder} дн., виртуальный остаток "
            f"{SUPPLIER_PREORDER_STOCK_COUNT}."
        ),
        "write_allowed": True,
        "stock_count": int(SUPPLIER_PREORDER_STOCK_COUNT),
        "preorder_days": preorder,
        "fulfillment_mode": "preorder",
        "source_kind": "supplier",
        "source_name": source.name,
    }


def _off_decision(state: FastDumpingState) -> dict[str, Any]:
    return {
        "safe_floor_kzt": None,
        "competitor_price_kzt": _money(state.competitor_price_kzt),
        "own_price_kzt": _money(state.own_price_kzt),
        "target_price_kzt": _money(state.own_price_kzt),
        "status": "zero_state",
        "reason": "FIFO закончился и доступного поставщика нет: SKU переводится в realtime 0/0.",
        "write_allowed": True,
        "stock_count": 0,
        "preorder_days": 0,
        "fulfillment_mode": "off",
        "source_kind": None,
    }


def _complete_scan_v2(
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
    original = _ORIGINALS["complete_scan"]
    result = original(
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
    if not succeeded:
        return result

    job = db.get(FastDumpingJob, job_id)
    if job is None or job.workspace_id != workspace_id:
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
    if state is None or policy is None or product is None or not policy.enabled or not product.sale_enabled:
        return result

    stock = physical_stock_count(db, product_id=product.id)
    source = resolve_cost_source(db, product_id=product.id, inventory_first=True)

    if stock > 0 and source is not None and source.kind == "inventory":
        state.inventory_on_hand = stock
        state.desired_stock_count = stock
        state.source_kind = "inventory"
        if result.get("queued_apply"):
            job.decision_json = _inventory_decision(job, state, stock)
            return result
        if state.status in SAFE_INVENTORY_SYNC_STATUSES and state.own_price_kzt is not None:
            decision = _inventory_decision(job, state, stock)
            _reactivate_apply(
                state=state,
                job=job,
                decision=decision,
                reason=(
                    "Цена не требует нового движения, но Fast Agent сверит realtime "
                    "stockCount/preOrder с Kaspi перед следующим write."
                ),
            )
            return {"status": state.status, "queued_apply": True, "decision": decision}
        return result

    if source is not None and source.kind == "supplier":
        decision = _supplier_decision(state=state, policy=policy, source=source)
        state.inventory_on_hand = 0
        state.desired_stock_count = int(SUPPLIER_PREORDER_STOCK_COUNT)
        state.source_kind = "supplier"
        state.source_name = source.name
        state.source_cost_kzt = source.unit_cost_kzt
        state.safe_floor_kzt = Decimal(str(decision["safe_floor_kzt"]))
        state.target_price_kzt = Decimal(str(decision["target_price_kzt"]))
        state.decision_status = "preorder_ready"
        _reactivate_apply(
            state=state,
            job=job,
            decision=decision,
            reason=decision["reason"],
        )
        return {"status": state.status, "queued_apply": True, "decision": decision}

    decision = _off_decision(state)
    state.inventory_on_hand = 0
    state.desired_stock_count = 0
    state.source_kind = None
    state.source_name = None
    state.source_cost_kzt = None
    state.safe_floor_kzt = None
    state.decision_status = "zero_state"
    _reactivate_apply(
        state=state,
        job=job,
        decision=decision,
        reason=decision["reason"],
    )
    return {"status": state.status, "queued_apply": True, "decision": decision}


def _stale_offer_job(
    *,
    state: FastDumpingState,
    job: FastDumpingJob,
    reason: str,
) -> dict[str, Any]:
    now = svc.utcnow()
    job.status = "stale"
    job.error_code = "stale_offer_state"
    job.error_message = reason
    job.completed_at = now
    job.lease_until = None
    job.lease_token = None
    job.not_before_at = None
    if state.active_job_id == job.id:
        state.active_job_id = None
    state.state_version += 1
    state.status = "stale"
    state.status_reason = reason
    state.last_error_code = "stale_offer_state"
    state.last_error_message = reason
    state.next_scan_at = now
    return {"ready": False, "stale": True, "reason": reason}


def _prepare_apply_v2(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
) -> dict[str, Any]:
    job = db.get(FastDumpingJob, job_id)
    mode = str((job.decision_json or {}).get("fulfillment_mode") or "inventory") if job else "inventory"
    if mode == "inventory":
        result = _ORIGINALS["prepare_apply"](
            db,
            workspace_id=workspace_id,
            job_id=job_id,
            agent_id=agent_id,
            lease_token=lease_token,
        )
        if result.get("ready"):
            result["fulfillment_mode"] = "inventory"
            result["preorder_days"] = 0
        return result

    job = svc._validate_lease(
        job,
        workspace_id=workspace_id,
        agent_id=agent_id,
        lease_token=lease_token,
        expected_status="leased_apply",
    )
    state = svc._lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
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
        raise ValueError("Fast dumping product state is inconsistent")
    if state.active_job_id != job.id or state.state_version != job.state_version:
        return _stale_offer_job(state=state, job=job, reason="Realtime offer-state уже заменён новой версией.")
    if not policy.enabled or not product.sale_enabled or state.automatic_writes_paused:
        return _stale_offer_job(state=state, job=job, reason="Товар или realtime Fast Dumping сейчас выключен.")

    stock = physical_stock_count(db, product_id=product.id)
    source = resolve_cost_source(db, product_id=product.id, inventory_first=True)
    decision = dict(job.decision_json or {})
    if mode == "preorder":
        if stock > 0 or source is None or source.kind != "supplier":
            return _stale_offer_job(
                state=state,
                job=job,
                reason="Источник изменился после scan: предзаказ больше не является актуальным режимом.",
            )
        preorder = max(1, _clamp_preorder(source.delivery_days))
        if preorder != int(decision.get("preorder_days") or 0):
            return _stale_offer_job(
                state=state,
                job=job,
                reason=(
                    f"Срок поставщика изменился после scan: было {decision.get('preorder_days')}, "
                    f"сейчас {preorder}. Выполняется новый scan."
                ),
            )
        desired_stock = int(SUPPLIER_PREORDER_STOCK_COUNT)
        state.source_kind = "supplier"
        state.source_name = source.name
        state.source_cost_kzt = source.unit_cost_kzt
    else:
        if stock > 0 or source is not None:
            return _stale_offer_job(
                state=state,
                job=job,
                reason="Появился FIFO или поставщик; zero-state отменён и будет пересчитан.",
            )
        preorder = 0
        desired_stock = 0

    state.status = "applying"
    state.status_reason = (
        "Fast Agent сверяет текущее Merchant offer-state перед единственной realtime mutation."
    )
    state.inventory_on_hand = stock
    state.desired_stock_count = desired_stock
    return {
        "ready": True,
        "job_id": job.id,
        "lease_token": job.lease_token,
        "state_version": state.state_version,
        "sku": product.merchant_sku or product.kaspi_product_id,
        "model": state.product_model or product.name,
        "city_id": policy.city_id,
        "zone_id": policy.zone_id,
        "target_price_kzt": decision.get("target_price_kzt"),
        "stock_count": desired_stock,
        "preorder_days": preorder,
        "fulfillment_mode": mode,
    }


def _complete_apply_v2(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    write_payload: dict[str, Any],
) -> dict[str, Any]:
    result = _ORIGINALS["complete_apply"](
        db,
        workspace_id=workspace_id,
        job_id=job_id,
        agent_id=agent_id,
        lease_token=lease_token,
        write_payload=write_payload,
    )
    job = db.get(FastDumpingJob, job_id)
    state = db.scalar(
        select(FastDumpingState).where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == (job.product_id if job else -1),
        )
    )
    if job is None or state is None:
        return result
    if bool(write_payload.get("accepted")) and not bool(write_payload.get("verified")):
        verify_at = svc.utcnow() + timedelta(seconds=OFFER_VERIFY_DELAY_SECONDS)
        job.not_before_at = verify_at
        state.next_scan_at = verify_at
        state.status = "verifying"
        state.status_reason = (
            "Kaspi принял realtime offer-state. Один SKU остаётся single-flight; "
            "Merchant BFF будет проверен через 15 секунд."
        )
        if write_payload.get("error_code") == "waiting_existing_operation":
            state.status_reason = (
                "Kaspi уже обрабатывает предыдущую mutation этого SKU. Новый write не отправлен; "
                "Fast Dumping ждёт завершения single-flight операции."
            )
    elif not bool(write_payload.get("accepted")) and write_payload.get("error_code") in {
        "kaspi_stock_lower_than_crm",
        "kaspi_zero_vs_crm_stock",
        "kaspi_offer_read_failed",
    }:
        state.status = "verification_retry"
        state.status_reason = str(write_payload.get("error_message") or "Расхождение offer-state Kaspi и CRM")
        state.last_error_code = str(write_payload.get("error_code"))
        state.last_error_message = state.status_reason
        state.next_scan_at = svc.utcnow()
    return result


def _serialize_claimed_job_v2(
    db: Session,
    *,
    job: FastDumpingJob,
    workspace_id: int,
) -> dict[str, Any]:
    payload = _ORIGINALS["serialize_claimed_job"](
        db,
        job=job,
        workspace_id=workspace_id,
    )
    if payload.get("stage") == "verify":
        decision = dict(job.decision_json or {})
        payload.update(
            {
                "target_price_kzt": decision.get("target_price_kzt"),
                "target_stock_count": decision.get("stock_count"),
                "target_preorder_days": decision.get("preorder_days", 0),
                "fulfillment_mode": decision.get("fulfillment_mode", "inventory"),
            }
        )
    return payload


def _complete_verification_v2(
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
    job = db.get(FastDumpingJob, job_id)
    state = svc._lock_state(db, workspace_id=workspace_id, product_id=job.product_id) if job else None
    if (
        job is not None
        and state is not None
        and not verification_succeeded
        and error_code == "kaspi_operation_in_progress"
    ):
        svc._validate_lease(
            job,
            workspace_id=workspace_id,
            agent_id=agent_id,
            lease_token=lease_token,
            expected_status="leased_verify",
        )
        write_json = dict(job.write_json or {})
        attempts = int(write_json.get("offer_verify_attempts") or 0) + 1
        write_json["offer_verify_attempts"] = attempts
        if error_message:
            write_json["last_offer_verify"] = str(error_message)[:2000]
        job.write_json = write_json
        if attempts <= OFFER_VERIFY_MAX_ATTEMPTS:
            verify_at = svc.utcnow() + timedelta(seconds=OFFER_VERIFY_DELAY_SECONDS)
            job.status = "queued_verify"
            job.agent_id = None
            job.lease_until = None
            job.lease_token = None
            job.not_before_at = verify_at
            state.status = "verifying"
            state.status_reason = (
                f"Kaspi mutation IN_PROGRESS (проверка {attempts}/{OFFER_VERIFY_MAX_ATTEMPTS}). "
                "Новый write по SKU заблокирован до завершения."
            )
            state.next_scan_at = verify_at
            return {"status": state.status, "verified": False, "pending": True, "verify_at": verify_at}

    result = _ORIGINALS["complete_verification"](
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
    job = db.get(FastDumpingJob, job_id)
    if job is not None:
        state = db.scalar(
            select(FastDumpingState).where(
                FastDumpingState.workspace_id == workspace_id,
                FastDumpingState.product_id == job.product_id,
            )
        )
        if state is not None:
            state.next_scan_at = svc.utcnow()
            if not verification_succeeded:
                state.last_error_code = error_code or "offer_state_mismatch"
                state.last_error_message = error_message
                state.status_reason = (
                    "Kaspi offer-state не совпал с желаемым после завершения mutation. "
                    "CRM поставила немедленный новый scan; детали показаны в Fast Dumping."
                )
    return result


def _fast_policy_enabled(db: Session, *, product_id: int) -> bool:
    try:
        policy = db.scalar(
            select(FastDumpingPolicy).where(
                FastDumpingPolicy.product_id == product_id,
                FastDumpingPolicy.enabled.is_(True),
            )
        )
        return policy is not None
    except Exception:
        return False


def _sync_product_inventory_to_feed_v2(
    db: Session,
    *,
    product_id: int,
    reason: str,
) -> dict[str, int | str | None]:
    if _fast_policy_enabled(db, product_id=product_id):
        stock = physical_stock_count(db, product_id=product_id)
        return {
            "stock_count": stock,
            "xml_state": "fast_realtime_owned",
            "supplier_job_id": None,
        }
    return _CLASSIC_ORIGINALS["sync_product_inventory_to_feed"](
        db,
        product_id=product_id,
        reason=reason,
    )


def install_fast_dumping_offer_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    for name in (
        "complete_scan",
        "prepare_apply",
        "complete_apply",
        "complete_verification",
        "serialize_claimed_job",
    ):
        _ORIGINALS[name] = getattr(svc, name)
    svc.complete_scan = _complete_scan_v2
    svc.prepare_apply = _prepare_apply_v2
    svc.complete_apply = _complete_apply_v2
    svc.complete_verification = _complete_verification_v2
    svc.serialize_claimed_job = _serialize_claimed_job_v2

    _CLASSIC_ORIGINALS["sync_product_inventory_to_feed"] = classic_dumping.sync_product_inventory_to_feed
    classic_dumping.sync_product_inventory_to_feed = _sync_product_inventory_to_feed_v2
