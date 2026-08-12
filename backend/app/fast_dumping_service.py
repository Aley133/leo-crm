from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from .dumping_service import (
    calculate_safe_floor,
    physical_stock_count,
    resolve_cost_source,
)
from .fast_dumping_models import (
    FastDumpingJob,
    FastDumpingPolicy,
    FastDumpingState,
)
from .fast_dumping_pricing import FastPriceDecision, decide_fast_price
from .models import Product


ACTIVE_JOB_STATUSES = {
    "queued_scan",
    "leased_scan",
    "queued_apply",
    "leased_apply",
    "queued_verify",
    "leased_verify",
}
QUEUED_JOB_STATUSES = ("queued_apply", "queued_verify", "queued_scan")
SCAN_LEASE_SECONDS = 180
APPLY_LEASE_SECONDS = 300
VERIFY_LEASE_SECONDS = 180
MAX_SCAN_ATTEMPTS = 3
MAX_MARKET_OFFERS = 100


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decimal(value: object, *, field: str, required: bool = False) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not result.is_finite() or result <= 0 or result > Decimal("1000000000"):
        raise ValueError(f"{field} is outside the accepted range")
    return result.quantize(Decimal("0.01"))


def _text(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered[:limit] or None


def _json_money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def normalize_market_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept only bounded, non-secret market facts from the local agent."""

    raw_offers = payload.get("offers")
    offers: list[dict[str, Any]] = []
    if isinstance(raw_offers, list):
        for raw in raw_offers[:MAX_MARKET_OFFERS]:
            if not isinstance(raw, dict):
                continue
            price_fields = raw.get("price_fields")
            safe_price_fields = {
                str(key)[:80]: str(value)[:120]
                for key, value in (price_fields.items() if isinstance(price_fields, dict) else [])
            }
            offers.append(
                {
                    "merchant_id": _text(raw.get("merchant_id"), limit=128),
                    "merchant_name": _text(raw.get("merchant_name"), limit=255),
                    "is_own": bool(raw.get("is_own")),
                    "price_kzt": _json_money(
                        _decimal(raw.get("price_kzt"), field="offer.price_kzt")
                    ),
                    "used_for_dumping": bool(raw.get("used_for_dumping")),
                    "ignored_reason": _text(raw.get("ignored_reason"), limit=500),
                    "price_fields": safe_price_fields,
                    "delivery": _text(raw.get("delivery"), limit=500),
                }
            )

    product_url = _text(payload.get("product_url"), limit=2000)
    if product_url and not product_url.startswith("https://kaspi.kz/"):
        product_url = None
    own_position = payload.get("own_position")
    seller_count = payload.get("seller_count")
    return {
        "product_name": _text(payload.get("product_name"), limit=500),
        "product_brand": _text(payload.get("product_brand"), limit=255),
        "own_price_kzt": _json_money(
            _decimal(payload.get("own_price_kzt"), field="own_price_kzt")
        ),
        "competitor_price_kzt": _json_money(
            _decimal(payload.get("competitor_price_kzt"), field="competitor_price_kzt")
        ),
        "competitor_name": _text(payload.get("competitor_name"), limit=255),
        "own_position": (
            int(own_position)
            if own_position not in (None, "") and 0 < int(own_position) <= 100000
            else None
        ),
        "seller_count": (
            int(seller_count)
            if seller_count not in (None, "") and 0 <= int(seller_count) <= 100000
            else 0
        ),
        "product_url": product_url,
        "own_delivery": _text(payload.get("own_delivery"), limit=500),
        "competitor_delivery": _text(payload.get("competitor_delivery"), limit=500),
        "offers": offers,
        "page_visible_price_kzt": _json_money(
            _decimal(payload.get("page_visible_price_kzt"), field="page_visible_price_kzt")
        ),
        "market_context_ok": bool(payload.get("market_context_ok")),
        "market_context_reason": _text(
            payload.get("market_context_reason"), limit=2000
        ),
    }


def ensure_state(
    db: Session,
    *,
    policy: FastDumpingPolicy,
    workspace_id: int,
) -> FastDumpingState:
    state = db.scalar(
        select(FastDumpingState).where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == policy.product_id,
        )
    )
    if state is None:
        state = FastDumpingState(
            workspace_id=workspace_id,
            policy_id=policy.id,
            product_id=policy.product_id,
            status="idle",
            next_scan_at=utcnow(),
        )
        db.add(state)
        db.flush()
    elif state.policy_id != policy.id:
        state.policy_id = policy.id
    return state


def _lock_state(
    db: Session,
    *,
    workspace_id: int,
    product_id: int,
) -> FastDumpingState | None:
    return db.scalar(
        select(FastDumpingState)
        .where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == product_id,
        )
        .with_for_update()
    )


def _clear_active_job(state: FastDumpingState, job: FastDumpingJob) -> None:
    if state.active_job_id == job.id:
        state.active_job_id = None


def _next_scan(policy: FastDumpingPolicy, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=max(8, int(policy.scan_interval_seconds)))


def queue_scan(
    db: Session,
    *,
    policy: FastDumpingPolicy,
    workspace_id: int,
    reason: str,
) -> tuple[FastDumpingJob | None, bool]:
    state = _lock_state(
        db,
        workspace_id=workspace_id,
        product_id=policy.product_id,
    )
    if state is None:
        state = ensure_state(db, policy=policy, workspace_id=workspace_id)
        state = _lock_state(
            db,
            workspace_id=workspace_id,
            product_id=policy.product_id,
        ) or state
    if state.active_job_id is not None:
        active = db.get(FastDumpingJob, state.active_job_id)
        if (
            active is not None
            and active.workspace_id == workspace_id
            and active.status in ACTIVE_JOB_STATUSES
        ):
            return active, False
        state.active_job_id = None
    if not policy.enabled or state.automatic_writes_paused:
        return None, False

    job = FastDumpingJob(
        workspace_id=workspace_id,
        policy_id=policy.id,
        product_id=policy.product_id,
        status="queued_scan",
        reason=_text(reason, limit=128),
    )
    db.add(job)
    db.flush()
    state.active_job_id = job.id
    state.status = "queued"
    state.status_reason = "Ожидает локальный Fast Dumping Agent."
    state.next_scan_at = None
    state.last_error_code = None
    state.last_error_message = None
    return job, True


def cancel_active_job(
    db: Session,
    *,
    state: FastDumpingState,
    reason: str,
) -> None:
    if state.active_job_id is None:
        return
    job = db.get(FastDumpingJob, state.active_job_id)
    if job is not None and job.status in ACTIVE_JOB_STATUSES:
        job.status = "cancelled"
        job.error_code = "configuration_changed"
        job.error_message = _text(reason, limit=2000)
        job.lease_until = None
        job.lease_token = None
        job.completed_at = utcnow()
    state.active_job_id = None
    state.state_version += 1


def recover_expired_leases(
    db: Session,
    *,
    workspace_id: int,
    now: datetime | None = None,
) -> int:
    checked_at = now or utcnow()
    jobs = db.scalars(
        select(FastDumpingJob)
        .where(
            FastDumpingJob.workspace_id == workspace_id,
            FastDumpingJob.status.in_(
                ("leased_scan", "leased_apply", "leased_verify")
            ),
            FastDumpingJob.lease_until.is_not(None),
            FastDumpingJob.lease_until < checked_at,
        )
        .with_for_update(skip_locked=True)
    ).all()
    recovered = 0
    for job in jobs:
        state = _lock_state(
            db, workspace_id=workspace_id, product_id=job.product_id
        )
        if state is None or state.active_job_id != job.id:
            job.status = "cancelled"
            job.completed_at = checked_at
            recovered += 1
            continue
        if job.status == "leased_scan" and job.scan_attempts < MAX_SCAN_ATTEMPTS:
            job.status = "queued_scan"
            job.agent_id = None
            job.lease_token = None
            job.lease_until = None
            state.status = "queued"
            state.status_reason = "Сканирование прервалось и будет безопасно повторено."
        elif job.status == "leased_apply":
            # The write may already have reached Kaspi. Verify first; never
            # submit the same unknown operation automatically a second time.
            job.status = "queued_verify"
            job.agent_id = None
            job.lease_token = None
            job.lease_until = None
            state.status = "verifying"
            state.status_reason = "Проверяем результат прерванной записи без повтора."
        else:
            job.status = "apply_unconfirmed"
            job.completed_at = checked_at
            job.error_code = "lease_expired"
            job.error_message = "Агент не подтвердил результат операции."
            _clear_active_job(state, job)
            state.status = "apply_unconfirmed"
            state.automatic_writes_paused = True
            state.pause_reason = (
                "Результат записи не подтверждён. Проверьте цену и нажмите «Возобновить»."
            )
            state.last_error_code = job.error_code
            state.last_error_message = job.error_message
        recovered += 1
    return recovered


def schedule_due_scans(
    db: Session,
    *,
    workspace_id: int,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    checked_at = now or utcnow()
    states = db.scalars(
        select(FastDumpingState)
        .join(
            FastDumpingPolicy,
            FastDumpingPolicy.id == FastDumpingState.policy_id,
        )
        .join(Product, Product.id == FastDumpingState.product_id)
        .where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingPolicy.workspace_id == workspace_id,
            Product.workspace_id == workspace_id,
            FastDumpingPolicy.enabled.is_(True),
            Product.sale_enabled.is_(True),
            FastDumpingState.active_job_id.is_(None),
            FastDumpingState.automatic_writes_paused.is_(False),
            or_(
                FastDumpingState.next_scan_at.is_(None),
                FastDumpingState.next_scan_at <= checked_at,
            ),
        )
        .order_by(FastDumpingState.next_scan_at, FastDumpingState.id)
        .limit(max(1, min(100, int(limit))))
        .with_for_update(skip_locked=True)
    ).all()
    queued = 0
    for state in states:
        policy = db.get(FastDumpingPolicy, state.policy_id)
        if policy is None or policy.workspace_id != workspace_id:
            continue
        _job, created = queue_scan(
            db,
            policy=policy,
            workspace_id=workspace_id,
            reason="scheduled",
        )
        queued += int(created)
    return queued


def claim_job(
    db: Session,
    *,
    workspace_id: int,
    agent_id: str,
) -> FastDumpingJob | None:
    recover_expired_leases(db, workspace_id=workspace_id)
    schedule_due_scans(db, workspace_id=workspace_id)
    priority = case(
        (FastDumpingJob.status == "queued_apply", 0),
        (FastDumpingJob.status == "queued_verify", 1),
        else_=2,
    )
    job = db.scalar(
        select(FastDumpingJob)
        .where(
            FastDumpingJob.workspace_id == workspace_id,
            FastDumpingJob.status.in_(QUEUED_JOB_STATUSES),
        )
        .order_by(priority, FastDumpingJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
    if state is None or state.active_job_id != job.id:
        job.status = "cancelled"
        job.completed_at = utcnow()
        return None

    now = utcnow()
    job.agent_id = _text(agent_id, limit=255)
    job.lease_token = uuid4().hex
    if job.status == "queued_scan":
        job.status = "leased_scan"
        job.scan_attempts += 1
        job.lease_until = now + timedelta(seconds=SCAN_LEASE_SECONDS)
        state.status = "scanning"
        state.status_reason = "Fast Agent проверяет карточку и офферы Kaspi."
    elif job.status == "queued_apply":
        job.status = "leased_apply"
        job.apply_attempts += 1
        job.lease_until = now + timedelta(seconds=APPLY_LEASE_SECONDS)
        state.status = "preparing_apply"
        state.status_reason = "CRM повторно сверяет floor и FIFO-остаток."
    else:
        job.status = "leased_verify"
        job.lease_until = now + timedelta(seconds=VERIFY_LEASE_SECONDS)
        state.status = "verifying"
        state.status_reason = "Проверяем нашу цену без повторной записи."
    state.last_agent_id = job.agent_id
    return job


def serialize_claimed_job(
    db: Session,
    *,
    job: FastDumpingJob,
    workspace_id: int,
) -> dict[str, Any]:
    product = db.scalar(
        select(Product).where(
            Product.id == job.product_id,
            Product.workspace_id == workspace_id,
        )
    )
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == workspace_id,
        )
    )
    if product is None or policy is None:
        raise ValueError("Fast dumping job ownership is inconsistent")
    if job.status == "leased_scan":
        stage = "scan"
    elif job.status == "leased_apply":
        stage = "apply"
    else:
        stage = "verify"
    payload: dict[str, Any] = {
        "id": job.id,
        "lease_token": job.lease_token,
        "stage": stage,
        "workspace_id": workspace_id,
        "product_id": product.id,
        "name": product.name,
        "brand": product.brand,
        "kaspi_product_id": product.kaspi_product_id,
        "merchant_sku": product.merchant_sku,
        "city_id": policy.city_id,
        "zone_id": policy.zone_id,
    }
    if stage == "verify":
        payload["target_price_kzt"] = (job.decision_json or {}).get(
            "target_price_kzt"
        )
    return payload


def _validate_lease(
    job: FastDumpingJob | None,
    *,
    workspace_id: int,
    agent_id: str,
    lease_token: str,
    expected_status: str,
) -> FastDumpingJob:
    if job is None or job.workspace_id != workspace_id:
        raise ValueError("Fast dumping job not found")
    if job.status != expected_status:
        raise ValueError(f"Job is not in {expected_status}")
    if job.agent_id != agent_id or not job.lease_token or job.lease_token != lease_token:
        raise ValueError("Job lease does not belong to this agent")
    if _aware(job.lease_until) is None or _aware(job.lease_until) < utcnow():
        raise ValueError("Job lease has expired")
    return job


def _finish_without_write(
    *,
    state: FastDumpingState,
    job: FastDumpingJob,
    policy: FastDumpingPolicy,
    status: str,
    reason: str,
    now: datetime,
) -> None:
    job.status = status
    job.completed_at = now
    job.lease_until = None
    job.lease_token = None
    _clear_active_job(state, job)
    state.status = status
    state.status_reason = reason
    state.next_scan_at = _next_scan(policy, now=now)


def complete_scan(
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
    job = _validate_lease(
        db.get(FastDumpingJob, job_id),
        workspace_id=workspace_id,
        agent_id=agent_id,
        lease_token=lease_token,
        expected_status="leased_scan",
    )
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
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
    now = utcnow()
    if not succeeded:
        job.status = "failed"
        job.error_code = _text(error_code, limit=128) or "scan_failed"
        job.error_message = _text(error_message, limit=4000) or "Kaspi scan failed"
        job.completed_at = now
        job.lease_until = None
        job.lease_token = None
        _clear_active_job(state, job)
        state.status = "error"
        state.status_reason = "Не удалось прочитать рынок Kaspi."
        state.last_error_code = job.error_code
        state.last_error_message = job.error_message
        state.next_scan_at = now + timedelta(
            seconds=max(30, int(policy.scan_interval_seconds))
        )
        return {"status": state.status, "queued_apply": False}

    market = normalize_market_snapshot(market_payload or {})
    job.market_json = market
    state.last_scanned_at = now
    state.own_price_kzt = _decimal(market.get("own_price_kzt"), field="own_price_kzt")
    state.competitor_price_kzt = _decimal(
        market.get("competitor_price_kzt"), field="competitor_price_kzt"
    )
    state.competitor_name = market.get("competitor_name")
    state.own_position = market.get("own_position")
    state.seller_count = market.get("seller_count")
    state.product_url = market.get("product_url")
    state.product_model = market.get("product_name") or product.name
    state.page_visible_price_kzt = _decimal(
        market.get("page_visible_price_kzt"), field="page_visible_price_kzt"
    )
    state.market_context_ok = bool(market.get("market_context_ok"))
    state.market_context_reason = market.get("market_context_reason")
    state.offers_json = market.get("offers") or []
    state.offers_count = len(state.offers_json)
    state.last_error_code = None
    state.last_error_message = None
    state.state_version += 1

    if not policy.enabled or not product.sale_enabled:
        reason = (
            "Быстрый демпинг выключен."
            if not policy.enabled
            else "Товар снят с продажи."
        )
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="paused",
            reason=reason,
            now=now,
        )
        return {"status": state.status, "queued_apply": False}
    if state.automatic_writes_paused:
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="apply_unconfirmed",
            reason=state.pause_reason or "Автозапись приостановлена.",
            now=now,
        )
        state.next_scan_at = None
        return {"status": state.status, "queued_apply": False}

    stock_count = physical_stock_count(db, product_id=product.id)
    source = resolve_cost_source(db, product_id=product.id, inventory_first=True)
    state.inventory_on_hand = stock_count
    state.desired_stock_count = stock_count
    if source is not None:
        state.source_kind = source.kind
        state.source_name = source.name
        state.source_cost_kzt = source.unit_cost_kzt
    else:
        state.source_kind = None
        state.source_name = None
        state.source_cost_kzt = None
    if stock_count <= 0 or source is None or source.kind != "inventory":
        state.safe_floor_kzt = None
        state.target_price_kzt = state.own_price_kzt
        state.decision_status = "out_of_stock"
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="out_of_stock",
            reason=(
                "Быстрый демпинг работает только с фактическим FIFO-остатком. "
                "Предзаказ и XML не изменялись."
            ),
            now=now,
        )
        return {"status": state.status, "queued_apply": False}

    floor = calculate_safe_floor(
        unit_cost_kzt=source.unit_cost_kzt,
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    state.safe_floor_kzt = floor
    if not state.market_context_ok:
        state.target_price_kzt = state.own_price_kzt
        state.decision_status = "market_context_mismatch"
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="market_context_mismatch",
            reason=(
                state.market_context_reason
                or "Публичная цена и Offers API относятся к разным контекстам."
            ),
            now=now,
        )
        return {"status": state.status, "queued_apply": False}
    if state.own_price_kzt is None:
        state.target_price_kzt = None
        state.decision_status = "own_offer_missing"
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="own_offer_missing",
            reason="Наша строка продавца не найдена; realtime-запись заблокирована.",
            now=now,
        )
        state.automatic_writes_paused = True
        state.pause_reason = (
            "Наша строка продавца не найдена. Проверьте Merchant UID, карточку "
            "и наличие товара в кабинете Kaspi, затем возобновите вручную."
        )
        state.next_scan_at = None
        return {"status": state.status, "queued_apply": False}

    decision = decide_fast_price(
        own_price_kzt=state.own_price_kzt,
        competitor_price_kzt=state.competitor_price_kzt,
        safe_floor_kzt=floor,
        undercut_step_kzt=Decimal(policy.undercut_step_kzt),
        allow_price_raise=policy.allow_price_raise,
        max_undercut_gap_percent=Decimal(policy.max_undercut_gap_percent),
    )
    state.target_price_kzt = decision.target_price_kzt
    state.decision_status = decision.status
    decision_json = _decision_json(decision, stock_count=stock_count)
    job.decision_json = decision_json
    job.state_version = state.state_version
    if not decision.write_allowed:
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status=decision.status,
            reason=decision.reason,
            now=now,
        )
        return {"status": state.status, "queued_apply": False, "decision": decision_json}
    if decision.target_price_kzt == decision.own_price_kzt:
        final_status = (
            "floor_limited" if decision.status == "floor_limited" else "watching"
        )
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status=final_status,
            reason=decision.reason,
            now=now,
        )
        return {"status": state.status, "queued_apply": False, "decision": decision_json}

    job.status = "queued_apply"
    job.agent_id = None
    job.lease_until = None
    job.lease_token = None
    state.status = "queued_apply"
    state.status_reason = (
        f"Готова realtime-цена {_json_money(decision.target_price_kzt)} ₸; "
        "ожидается повторная проверка остатка."
    )
    return {"status": state.status, "queued_apply": True, "decision": decision_json}


def _decision_json(
    decision: FastPriceDecision,
    *,
    stock_count: int,
) -> dict[str, Any]:
    return {
        "safe_floor_kzt": _json_money(decision.safe_floor_kzt),
        "competitor_price_kzt": _json_money(decision.competitor_price_kzt),
        "own_price_kzt": _json_money(decision.own_price_kzt),
        "target_price_kzt": _json_money(decision.target_price_kzt),
        "undercut_step_kzt": _json_money(decision.undercut_step_kzt),
        "status": decision.status,
        "reason": decision.reason,
        "write_allowed": decision.write_allowed,
        "gap_percent": _json_money(decision.gap_percent),
        "max_undercut_gap_percent": _json_money(
            decision.max_undercut_gap_percent
        ),
        "stock_count": int(stock_count),
    }


def prepare_apply(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
) -> dict[str, Any]:
    job = _validate_lease(
        db.get(FastDumpingJob, job_id),
        workspace_id=workspace_id,
        agent_id=agent_id,
        lease_token=lease_token,
        expected_status="leased_apply",
    )
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
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
    now = utcnow()

    stale_reason = None
    if state.active_job_id != job.id or state.state_version != job.state_version:
        stale_reason = "Решение уже заменено новой версией."
    elif not policy.enabled or not product.sale_enabled:
        stale_reason = "Товар или быстрый демпинг выключен."
    elif state.automatic_writes_paused:
        stale_reason = state.pause_reason or "Автозапись приостановлена."

    stock_count = physical_stock_count(db, product_id=product.id)
    source = resolve_cost_source(db, product_id=product.id, inventory_first=True)
    if stock_count <= 0 or source is None or source.kind != "inventory":
        stale_reason = "Фактический FIFO-остаток закончился; запись отменена."
    decision: FastPriceDecision | None = None
    if stale_reason is None:
        floor = calculate_safe_floor(
            unit_cost_kzt=source.unit_cost_kzt,
            minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
        )
        decision = decide_fast_price(
            own_price_kzt=_decimal(
                (job.market_json or {}).get("own_price_kzt"), field="own_price_kzt"
            ),
            competitor_price_kzt=_decimal(
                (job.market_json or {}).get("competitor_price_kzt"),
                field="competitor_price_kzt",
            ),
            safe_floor_kzt=floor,
            undercut_step_kzt=Decimal(policy.undercut_step_kzt),
            allow_price_raise=policy.allow_price_raise,
            max_undercut_gap_percent=Decimal(policy.max_undercut_gap_percent),
        )
        previous_target = _decimal(
            (job.decision_json or {}).get("target_price_kzt"),
            field="target_price_kzt",
        )
        previous_stock = int((job.decision_json or {}).get("stock_count") or 0)
        if (
            not decision.write_allowed
            or decision.target_price_kzt != previous_target
            or stock_count != previous_stock
        ):
            stale_reason = (
                "Floor, целевая цена или FIFO-остаток изменились после сканирования."
            )

    if stale_reason is not None or decision is None:
        job.status = "stale"
        job.error_code = "stale_decision"
        job.error_message = stale_reason
        job.completed_at = now
        job.lease_until = None
        job.lease_token = None
        _clear_active_job(state, job)
        state.state_version += 1
        state.status = "stale"
        state.status_reason = stale_reason
        state.inventory_on_hand = stock_count
        state.desired_stock_count = stock_count
        state.next_scan_at = now
        return {"ready": False, "stale": True, "reason": stale_reason}

    state.status = "applying"
    state.status_reason = "Операция передана Fast Agent; ожидается подтверждение нашей цены."
    state.inventory_on_hand = stock_count
    state.desired_stock_count = stock_count
    state.safe_floor_kzt = decision.safe_floor_kzt
    state.target_price_kzt = decision.target_price_kzt
    return {
        "ready": True,
        "job_id": job.id,
        "lease_token": job.lease_token,
        "state_version": state.state_version,
        "sku": product.merchant_sku or product.kaspi_product_id,
        "model": state.product_model or product.name,
        "city_id": policy.city_id,
        "zone_id": policy.zone_id,
        "target_price_kzt": _json_money(decision.target_price_kzt),
        "stock_count": stock_count,
    }


def complete_apply(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    write_payload: dict[str, Any],
) -> dict[str, Any]:
    job = _validate_lease(
        db.get(FastDumpingJob, job_id),
        workspace_id=workspace_id,
        agent_id=agent_id,
        lease_token=lease_token,
        expected_status="leased_apply",
    )
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == workspace_id,
        )
    )
    if state is None or policy is None:
        raise ValueError("Fast dumping product state is inconsistent")
    now = utcnow()
    verified = bool(write_payload.get("verified"))
    accepted = bool(write_payload.get("accepted"))
    operation_id = _text(write_payload.get("operation_id"), limit=255)
    status_code = write_payload.get("status_code")
    observed_price = _decimal(
        write_payload.get("observed_own_price_kzt"),
        field="observed_own_price_kzt",
    )
    safe_write = {
        "accepted": accepted,
        "verified": verified,
        "status_code": int(status_code) if status_code not in (None, "") else None,
        "operation_id": operation_id,
        "latency_seconds": float(write_payload.get("latency_seconds") or 0),
        "observed_own_price_kzt": _json_money(observed_price),
        "session_refreshed": bool(write_payload.get("session_refreshed")),
        "error_code": _text(write_payload.get("error_code"), limit=128),
        "error_message": _text(write_payload.get("error_message"), limit=2000),
    }
    job.write_json = safe_write
    job.lease_until = None
    job.lease_token = None
    job.completed_at = now
    _clear_active_job(state, job)
    state.last_operation_id = operation_id
    state.last_agent_id = agent_id

    if verified:
        job.status = "applied"
        state.status = (
            "floor_limited"
            if (job.decision_json or {}).get("status") == "floor_limited"
            else "applied"
        )
        state.status_reason = (
            "Цена применена и подтверждена по нашей строке продавца."
            if state.status == "applied"
            else "Цена подтверждена на floor; конкурент уже ниже безопасного порога."
        )
        state.last_applied_at = now
        state.last_error_code = None
        state.last_error_message = None
        state.next_scan_at = _next_scan(policy, now=now)
        return {"status": state.status, "verified": True}

    error_code = safe_write["error_code"] or (
        "apply_timeout" if accepted else "apply_failed"
    )
    error_message = safe_write["error_message"] or (
        "Kaspi принял операцию, но наша цена не подтвердилась."
        if accepted
        else "Kaspi отклонил realtime-запись."
    )
    job.status = error_code
    job.error_code = error_code
    job.error_message = error_message
    state.status = error_code
    state.status_reason = error_message
    state.last_error_code = error_code
    state.last_error_message = error_message
    if accepted:
        state.automatic_writes_paused = True
        state.pause_reason = (
            "Операция была принята, но итоговая цена не подтверждена. "
            "Проверьте Kaspi и возобновите товар вручную."
        )
        state.next_scan_at = None
    else:
        state.next_scan_at = now + timedelta(
            seconds=max(30, int(policy.scan_interval_seconds))
        )
    return {"status": state.status, "verified": False}


def complete_verification(
    db: Session,
    *,
    workspace_id: int,
    job_id: int,
    agent_id: str,
    lease_token: str,
    observed_own_price_kzt: object,
) -> dict[str, Any]:
    job = _validate_lease(
        db.get(FastDumpingJob, job_id),
        workspace_id=workspace_id,
        agent_id=agent_id,
        lease_token=lease_token,
        expected_status="leased_verify",
    )
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.id == job.policy_id,
            FastDumpingPolicy.workspace_id == workspace_id,
        )
    )
    if state is None or policy is None:
        raise ValueError("Fast dumping product state is inconsistent")
    target = _decimal(
        (job.decision_json or {}).get("target_price_kzt"), field="target_price_kzt"
    )
    observed = _decimal(observed_own_price_kzt, field="observed_own_price_kzt")
    now = utcnow()
    job.completed_at = now
    job.lease_until = None
    job.lease_token = None
    _clear_active_job(state, job)
    if target is not None and observed == target:
        job.status = "applied"
        state.status = (
            "floor_limited"
            if (job.decision_json or {}).get("status") == "floor_limited"
            else "applied"
        )
        state.status_reason = "Прерванная операция найдена и подтверждена без повтора."
        state.last_applied_at = now
        state.next_scan_at = _next_scan(policy, now=now)
        return {"status": state.status, "verified": True}

    job.status = "apply_unconfirmed"
    job.error_code = "apply_unconfirmed"
    job.error_message = "Результат прерванной операции не подтверждён."
    state.status = "apply_unconfirmed"
    state.status_reason = job.error_message
    state.automatic_writes_paused = True
    state.pause_reason = (
        "Цена после прерванной операции не подтверждена. Проверьте Kaspi и "
        "возобновите товар вручную."
    )
    state.next_scan_at = None
    return {"status": state.status, "verified": False}


def resume_automatic_writes(
    db: Session,
    *,
    workspace_id: int,
    product_id: int,
) -> FastDumpingState:
    state = _lock_state(db, workspace_id=workspace_id, product_id=product_id)
    if state is None:
        raise ValueError("Fast dumping state not found")
    if state.active_job_id is not None:
        raise ValueError("Товар уже обрабатывается")
    state.automatic_writes_paused = False
    state.pause_reason = None
    state.last_error_code = None
    state.last_error_message = None
    state.status = "idle"
    state.status_reason = "Защитная пауза снята вручную; ожидается новая проверка."
    state.next_scan_at = utcnow()
    return state
