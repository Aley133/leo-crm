from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, delete, or_, select, update
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
from .fast_dumping_pricing import (
    FastPriceDecision,
    decide_fast_price,
    external_anchor_price,
)
from .models import MarketplaceAccount, Product
from .product_images import normalize_product_image_url
from .workspace_models import KaspiAccountCredential, Workspace


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
MAX_MARKET_OFFERS = 200
MIN_SCAN_INTERVAL_SECONDS = 300
DEFAULT_SCAN_INTERVAL_SECONDS = 600
HISTORY_RETENTION_PER_PRODUCT = 100
HISTORY_PRUNE_INTERVAL_SECONDS = 3600
INVENTORY_RECOVERY_INTERVAL_SECONDS = 300
_HISTORY_PRUNE_LOCK = Lock()
_AUTO_SCAN_STREAK: dict[int, int] = {}
_NON_SCAN_STREAK: dict[int, int] = {}
_AUTO_QUEUE_LOCK = Lock()
_HISTORY_PRUNE_NOT_BEFORE: dict[int, float] = {}
_INVENTORY_RECOVERY_LOCK = Lock()
_INVENTORY_RECOVERY_NOT_BEFORE: dict[int, float] = {}
_SUPPLY_RECOVERY_NOT_BEFORE: dict[int, float] = {}


def _reserve_supply_recovery(workspace_id: int) -> bool:
    now = monotonic()
    with _INVENTORY_RECOVERY_LOCK:
        if _SUPPLY_RECOVERY_NOT_BEFORE.get(workspace_id, 0) > now:
            return False
        _SUPPLY_RECOVERY_NOT_BEFORE[workspace_id] = now + 60
        return True


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


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if minimum <= result <= maximum else None


def _json_money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _identity(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


def _owned_shop_identities(db: Session) -> tuple[list[str], list[str]]:
    rows = db.execute(
        select(
            KaspiAccountCredential.partner_id,
            Workspace.name,
            MarketplaceAccount.display_name,
        )
        .join(Workspace, Workspace.id == KaspiAccountCredential.workspace_id)
        .join(
            MarketplaceAccount,
            MarketplaceAccount.id == KaspiAccountCredential.marketplace_account_id,
        )
        .where(Workspace.is_active.is_(True))
        .order_by(Workspace.id)
        .limit(20)
        .execution_options(include_all_workspaces=True)
    ).all()
    merchant_ids = list(
        dict.fromkeys(
            str(partner_id).strip()
            for partner_id, _workspace_name, _display_name in rows
            if str(partner_id or "").strip()
        )
    )
    merchant_names = list(
        dict.fromkeys(
            name.strip()
            for _partner_id, workspace_name, display_name in rows
            for name in (str(workspace_name or ""), str(display_name or ""))
            if name.strip()
        )
    )
    return merchant_ids, merchant_names


def _refresh_owned_cycle_anchor(state: FastDumpingState) -> None:
    """Keep one durable reset point even when only the owner's shops remain."""

    external = external_anchor_price(
        state.offers_json or [],
        page_visible_price_kzt=state.page_visible_price_kzt,
    )
    if external is not None:
        state.owned_cycle_anchor_price_kzt = external
        return

    owned_prices: list[Decimal] = []
    if state.own_price_kzt is not None:
        owned_prices.append(Decimal(state.own_price_kzt))
    for offer in state.offers_json or []:
        if not isinstance(offer, dict) or bool(offer.get("is_own")):
            continue
        if not (bool(offer.get("is_owned_peer")) or bool(offer.get("is_owned_group"))):
            continue
        price = _decimal(offer.get("price_kzt"), field="offer.price_kzt")
        if price is not None:
            owned_prices.append(price)
    if not owned_prices:
        return

    current_start = max(owned_prices)
    if (
        state.owned_cycle_anchor_price_kzt is None
        or current_start > state.owned_cycle_anchor_price_kzt
    ):
        state.owned_cycle_anchor_price_kzt = current_start


def normalize_market_snapshot(
    payload: dict[str, Any],
    *,
    owned_merchant_ids: list[str] | tuple[str, ...] | None = None,
    owned_merchant_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Accept only bounded, non-secret market facts from the local agent."""

    known_owned_ids = {_identity(value) for value in (owned_merchant_ids or ())}
    known_owned_names = {_identity(value) for value in (owned_merchant_names or ())}
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
            merchant_id = _text(raw.get("merchant_id"), limit=128)
            merchant_name = _text(raw.get("merchant_name"), limit=255)
            is_own = bool(raw.get("is_own"))
            known_peer = not is_own and bool(
                (_identity(merchant_id) and _identity(merchant_id) in known_owned_ids)
                or (
                    _identity(merchant_name)
                    and _identity(merchant_name) in known_owned_names
                )
            )
            is_owned_peer = bool(raw.get("is_owned_peer")) or known_peer
            is_owned_group = (
                is_own or is_owned_peer or bool(raw.get("is_owned_group"))
            )
            offers.append(
                {
                    "merchant_id": merchant_id,
                    "merchant_name": merchant_name,
                    "is_own": is_own,
                    "own_match": _text(raw.get("own_match"), limit=32),
                    "is_owned_group": is_owned_group,
                    "is_owned_peer": is_owned_peer,
                    "owned_peer_match": _text(
                        raw.get("owned_peer_match"), limit=32
                    )
                    or ("crm_identity" if known_peer else None),
                    "price_kzt": _json_money(
                        _decimal(raw.get("price_kzt"), field="offer.price_kzt")
                    ),
                    "used_for_dumping": bool(raw.get("used_for_dumping"))
                    and not is_owned_group,
                    "ignored_reason": _text(raw.get("ignored_reason"), limit=500),
                    "decision_reason": _text(raw.get("decision_reason"), limit=500),
                    "price_fields": safe_price_fields,
                    "delivery": _text(raw.get("delivery"), limit=500),
                    "delivery_days": _bounded_int(
                        raw.get("delivery_days"), minimum=0, maximum=60
                    ),
                    "delivery_gap_days": _bounded_int(
                        raw.get("delivery_gap_days"), minimum=-60, maximum=60
                    ),
                    "price_gap_kzt": _text(raw.get("price_gap_kzt"), limit=40),
                }
            )

    product_url = _text(payload.get("product_url"), limit=2000)
    if product_url and not product_url.startswith("https://kaspi.kz/"):
        product_url = None
    own_position = payload.get("own_position")
    seller_count = payload.get("seller_count")
    return {
        "offers_complete": payload.get("offers_complete") is True and isinstance(raw_offers, list) and len(raw_offers) <= MAX_MARKET_OFFERS,
        "product_name": _text(payload.get("product_name"), limit=500),
        "product_brand": _text(payload.get("product_brand"), limit=255),
        "image_url": normalize_product_image_url(_text(payload.get("image_url"), limit=2048)),
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
        "own_delivery_days": _bounded_int(
            payload.get("own_delivery_days"), minimum=0, maximum=60
        ),
        "competitor_delivery_days": _bounded_int(
            payload.get("competitor_delivery_days"), minimum=0, maximum=60
        ),
        "delivery_filtered_count": (
            _bounded_int(payload.get("delivery_filtered_count"), minimum=0, maximum=100)
            or 0
        ),
        "delivery_selection_reason": _text(
            payload.get("delivery_selection_reason"), limit=2000
        ),
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


def _policy_interval_seconds(policy: FastDumpingPolicy) -> int:
    """Never let a legacy policy poll Kaspi more often than every five minutes."""

    try:
        configured = int(policy.scan_interval_seconds)
    except (TypeError, ValueError):
        configured = DEFAULT_SCAN_INTERVAL_SECONDS
    if getattr(policy, "pricing_mode", "manual") == "automation":
        return max(60, min(300, int((policy.automation_config or {}).get("monitor_seconds", 120))))
    return max(MIN_SCAN_INTERVAL_SECONDS, configured)


def _next_scan(policy: FastDumpingPolicy, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=_policy_interval_seconds(policy))


def _next_write_allowed_at(
    state: FastDumpingState,
    policy: FastDumpingPolicy,
) -> datetime | None:
    applied_at = _aware(state.last_applied_at)
    if applied_at is None:
        return None
    return applied_at + timedelta(seconds=max(300, _policy_interval_seconds(policy)))


def prune_fast_dumping_history(
    db: Session,
    *,
    workspace_id: int,
    per_product_limit: int = HISTORY_RETENTION_PER_PRODUCT,
) -> int:
    """Bound completed scan JSON history while preserving every active job."""

    keep = max(10, int(per_product_limit))
    product_ids = db.scalars(
        select(FastDumpingJob.product_id)
        .where(FastDumpingJob.workspace_id == workspace_id)
        .distinct()
    ).all()
    removed = 0
    for product_id in product_ids:
        cutoff_id = db.scalar(
            select(FastDumpingJob.id)
            .where(
                FastDumpingJob.workspace_id == workspace_id,
                FastDumpingJob.product_id == product_id,
                FastDumpingJob.completed_at.is_not(None),
            )
            .order_by(FastDumpingJob.id.desc())
            .offset(keep)
            .limit(1)
        )
        if cutoff_id is None:
            continue
        # Keep one confirmed fulfillment snapshot, even after many no-write
        # scans, so desired stock cannot be mistaken for applied Kaspi stock.
        confirmed_id = db.scalar(select(FastDumpingJob.id).where(
            FastDumpingJob.workspace_id == workspace_id,
            FastDumpingJob.product_id == product_id,
            FastDumpingJob.status == "applied",
        ).order_by(FastDumpingJob.completed_at.desc(), FastDumpingJob.id.desc()).limit(1))
        result = db.execute(
            delete(FastDumpingJob).where(
                FastDumpingJob.workspace_id == workspace_id,
                FastDumpingJob.product_id == product_id,
                FastDumpingJob.completed_at.is_not(None),
                FastDumpingJob.id <= cutoff_id,
                FastDumpingJob.id != confirmed_id if confirmed_id is not None else True,
            )
        )
        removed += int(result.rowcount or 0)
    return removed


def _maybe_prune_fast_dumping_history(
    db: Session,
    *,
    workspace_id: int,
    now: float | None = None,
) -> int:
    checked_at = monotonic() if now is None else now
    with _HISTORY_PRUNE_LOCK:
        if _HISTORY_PRUNE_NOT_BEFORE.get(workspace_id, 0.0) > checked_at:
            return 0
        _HISTORY_PRUNE_NOT_BEFORE[workspace_id] = (
            checked_at + HISTORY_PRUNE_INTERVAL_SECONDS
        )
    return prune_fast_dumping_history(db, workspace_id=workspace_id)


def _reserve_inventory_recovery(
    workspace_id: int,
    *,
    now: float | None = None,
) -> bool:
    """Rate-limit the legacy recovery sweep without delaying live FIFO events."""

    checked_at = monotonic() if now is None else now
    with _INVENTORY_RECOVERY_LOCK:
        if _INVENTORY_RECOVERY_NOT_BEFORE.get(workspace_id, 0.0) > checked_at:
            return False
        _INVENTORY_RECOVERY_NOT_BEFORE[workspace_id] = (
            checked_at + INVENTORY_RECOVERY_INTERVAL_SECONDS
        )
    return True


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
            if (reason == "manual" and active.status == "queued_scan"
                    and not str(active.reason or "").startswith("inventory_priority:")):
                active.reason = "manual"
                _mark_scan_queued(state, active)
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
    _mark_scan_queued(state, job)
    return job, True


def _mark_scan_queued(state: FastDumpingState, job: FastDumpingJob) -> None:
    state.active_job_id = job.id
    state.status = "queued"
    state.status_reason = "Ожидает локальный Fast Dumping Agent."
    state.next_scan_at = None
    state.last_error_code = None
    state.last_error_message = None


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
        .with_for_update(skip_locked=True, of=FastDumpingJob)
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
            job.not_before_at = None
            state.status = "queued"
            state.status_reason = "Сканирование прервалось и будет безопасно повторено."
        elif job.status == "leased_apply":
            policy = db.get(FastDumpingPolicy, job.policy_id)
            verify_delay = (
                _policy_interval_seconds(policy)
                if policy is not None and policy.workspace_id == workspace_id
                else DEFAULT_SCAN_INTERVAL_SECONDS
            )
            job.status = "queued_verify"
            job.agent_id = None
            job.lease_token = None
            job.lease_until = None
            job.not_before_at = checked_at + timedelta(seconds=verify_delay)
            state.status = "verifying"
            state.status_reason = (
                "Запись могла дойти до Kaspi; контрольная проверка отложена "
                "без повторной отправки цены."
            )
            state.next_scan_at = job.not_before_at
        elif job.status == "leased_scan":
            policy = db.get(FastDumpingPolicy, job.policy_id)
            retry_delay = (
                _policy_interval_seconds(policy)
                if policy is not None and policy.workspace_id == workspace_id
                else DEFAULT_SCAN_INTERVAL_SECONDS
            )
            job.status = "failed"
            job.completed_at = checked_at
            job.error_code = "scan_attempts_exhausted"
            job.error_message = "Сканирование исчерпало безопасный лимит повторов."
            job.lease_until = None
            job.lease_token = None
            job.not_before_at = None
            _clear_active_job(state, job)
            state.status = "error"
            state.status_reason = (
                "Сканирование несколько раз прервалось; новый scan будет "
                "выполнен после выбранного интервала."
            )
            state.last_error_code = job.error_code
            state.last_error_message = job.error_message
            state.next_scan_at = checked_at + timedelta(seconds=retry_delay)
        else:
            policy = db.get(FastDumpingPolicy, job.policy_id)
            retry_delay = (
                _policy_interval_seconds(policy)
                if policy is not None and policy.workspace_id == workspace_id
                else DEFAULT_SCAN_INTERVAL_SECONDS
            )
            job.status = "verification_failed"
            job.completed_at = checked_at
            job.error_code = "lease_expired"
            job.error_message = "Агент не завершил контрольную проверку цены."
            job.lease_until = None
            job.lease_token = None
            job.not_before_at = None
            _clear_active_job(state, job)
            state.status = "verification_retry"
            state.status_reason = (
                "Контрольная проверка прервалась. Старая операция не повторяется; "
                "после выбранного интервала будет выполнен новый полный scan."
            )
            state.last_error_code = job.error_code
            state.last_error_message = job.error_message
            state.automatic_writes_paused = False
            state.pause_reason = None
            state.next_scan_at = checked_at + timedelta(seconds=retry_delay)
        recovered += 1
    return recovered


def _schedule_inventory_transitions(db: Session, workspace_id: int, limit: int) -> int:
    # Recover arrivals missed by an older deployment; do not wait for the
    # regular price deadline, and never interrupt an in-flight SKU operation.
    # First select the small durable state table, then use the established
    # product/status/id job index for each candidate. This avoids a correlated
    # JSON/history scan over every row in fast_dumping_jobs on every claim.
    states = db.scalars(
        select(FastDumpingState)
        .join(FastDumpingPolicy, FastDumpingPolicy.id == FastDumpingState.policy_id)
        .join(Product, Product.id == FastDumpingState.product_id)
        .where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingPolicy.workspace_id == workspace_id,
            Product.workspace_id == workspace_id,
            FastDumpingPolicy.enabled.is_(True),
            Product.sale_enabled.is_(True),
            FastDumpingState.automatic_writes_paused.is_(False),
            FastDumpingState.inventory_on_hand > 0,
        )
        .order_by(FastDumpingState.id)
        .limit(max(1, min(100, int(limit))))
        .with_for_update(skip_locked=True, of=FastDumpingState)
    ).all()
    created_count = 0
    for state in states:
        last_applied_mode = db.scalar(
            select(FastDumpingJob.decision_json["fulfillment_mode"].as_string())
            .where(
                FastDumpingJob.workspace_id == workspace_id,
                FastDumpingJob.product_id == state.product_id,
                FastDumpingJob.status == "applied",
            )
            .order_by(FastDumpingJob.id.desc())
            .limit(1)
        )
        if last_applied_mode != "preorder":
            continue

        active = (
            db.get(FastDumpingJob, state.active_job_id)
            if state.active_job_id is not None
            else None
        )
        if active is not None and active.status in ACTIVE_JOB_STATUSES:
            if active.status != "queued_scan":
                continue
            active.reason = "inventory_priority:recover_existing_fifo"
            active.not_before_at = None
        else:
            state.active_job_id = None
            policy = db.get(FastDumpingPolicy, state.policy_id)
            if policy is None or policy.workspace_id != workspace_id:
                continue
            _, created = queue_scan(
                db,
                policy=policy,
                workspace_id=workspace_id,
                reason="inventory_priority:recover_existing_fifo",
            )
            created_count += int(created)
        state.next_scan_at = utcnow()
    return created_count


def schedule_due_scans(
    db: Session,
    *,
    workspace_id: int,
    limit: int = 20,
    now: datetime | None = None,
    recover_inventory_transitions: bool = True,
) -> int:
    checked_at = now or utcnow()
    priority_queued = (
        _schedule_inventory_transitions(db, workspace_id, limit)
        if recover_inventory_transitions
        else 0
    )
    candidates = db.execute(
        select(FastDumpingState, FastDumpingPolicy)
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
            or_(Product.sale_enabled.is_(True), Product.sale_state_overridden.is_(False)),
            FastDumpingState.active_job_id.is_(None),
            FastDumpingState.automatic_writes_paused.is_(False),
            or_(
                FastDumpingState.next_scan_at.is_(None),
                FastDumpingState.next_scan_at <= checked_at,
            ),
        )
        .order_by(case((FastDumpingState.last_scanned_at.is_(None), 0), else_=1),
                  case((FastDumpingPolicy.pricing_mode == "automation", 0), else_=1),
                  FastDumpingState.next_scan_at, FastDumpingState.id)
        .limit(max(1, min(100, int(limit))))
        .with_for_update(skip_locked=True, of=FastDumpingState)
    ).all()
    # These states and policies are already locked and have no active job.
    # Avoid re-reading both rows and flushing the entire session per product:
    # remote database round trips otherwise grow linearly with each due batch.
    pending = []
    for state, policy in candidates:
        job = FastDumpingJob(
            workspace_id=workspace_id,
            policy_id=policy.id,
            product_id=policy.product_id,
            status="queued_scan",
            reason="scheduled",
        )
        db.add(job)
        pending.append((state, job))
    if pending:
        db.flush()
        for state, job in pending:
            _mark_scan_queued(state, job)
        # Subsequent scheduling in this transaction must see the active job IDs
        # even though SessionLocal deliberately disables implicit autoflush.
        db.flush()
    return priority_queued + len(pending)


def claim_job(
    db: Session,
    *,
    workspace_id: int,
    agent_id: str,
) -> FastDumpingJob | None:
    from .preorder_modes import reconcile_preorder_policies
    reconcile_preorder_policies(db, workspace_id)
    if _reserve_supply_recovery(workspace_id):
        from .fast_dumping_supply_guard import disable_empty_products
        disable_empty_products(db, workspace_id)
    recover_expired_leases(db, workspace_id=workspace_id)
    # Retention over completed JSON history must never run inside the agent's
    # latency-sensitive claim transaction. The callable is kept for controlled
    # maintenance, but live workers only touch active queue/state rows.
    schedule_due_scans(
        db,
        workspace_id=workspace_id,
        recover_inventory_transitions=_reserve_inventory_recovery(workspace_id),
    )
    now = utcnow()
    with _AUTO_QUEUE_LOCK:
        scan_streak = _AUTO_SCAN_STREAK.get(workspace_id, 0)
        prefer_auto = scan_streak % 5 != 4
        prefer_scan = _NON_SCAN_STREAK.get(workspace_id, 0) >= 3
    is_scan = FastDumpingJob.status == "queued_scan"
    interactive_scan = and_(is_scan, FastDumpingJob.reason.in_(
        ("policy_saved", "automation_mode_changed", "manual", "manual_resume", "preorder_monitoring_enabled", "product_test_auto_enroll")
    ))
    aged_scan = and_(is_scan, FastDumpingJob.created_at <= now - timedelta(minutes=15),
                     scan_streak % 2 == 1)
    # User connections must not sit behind hours of periodic backlog. Reserve
    # a scan after three write/verify claims too, so confirmations cannot starve
    # every other card. Normal traffic retains the 4:1 rocket/ordinary share;
    # overdue scans get every other scan slot until the old backlog clears.
    priority = case(
        (FastDumpingJob.reason.startswith("inventory_priority:"), -3),
        (interactive_scan, -2),
        (and_(is_scan, prefer_scan), -1),
        (FastDumpingJob.status == "queued_apply", 0),
        (FastDumpingJob.status == "queued_verify", 1),
        (aged_scan, 2),
        (FastDumpingPolicy.pricing_mode == "automation", 3 if prefer_auto else 4),
        else_=4 if prefer_auto else 3,
    )
    job = db.scalar(
        select(FastDumpingJob)
        .join(FastDumpingPolicy, FastDumpingPolicy.id == FastDumpingJob.policy_id)
        .join(FastDumpingState, FastDumpingState.active_job_id == FastDumpingJob.id)
        .where(
            FastDumpingJob.workspace_id == workspace_id,
            FastDumpingPolicy.workspace_id == workspace_id,
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingJob.status.in_(QUEUED_JOB_STATUSES),
            or_(
                FastDumpingJob.not_before_at.is_(None),
                FastDumpingJob.not_before_at <= now,
            ),
        )
        .order_by(priority,
                  case((aged_scan, 0), else_=1),
                  case((and_(is_scan, FastDumpingState.last_scanned_at.is_(None)), 0), else_=1),
                  FastDumpingJob.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=(FastDumpingJob, FastDumpingState))
    )
    if job is None:
        return None
    state = _lock_state(db, workspace_id=workspace_id, product_id=job.product_id)
    if state is None or state.active_job_id != job.id:
        job.status = "cancelled"
        job.completed_at = utcnow()
        return None

    with _AUTO_QUEUE_LOCK:
        _NON_SCAN_STREAK[workspace_id] = (
            0 if job.status == "queued_scan" else _NON_SCAN_STREAK.get(workspace_id, 0) + 1
        )

    job.agent_id = _text(agent_id, limit=255)
    job.lease_token = uuid4().hex
    job.not_before_at = None
    if job.status == "queued_scan":
        with _AUTO_QUEUE_LOCK:
            _AUTO_SCAN_STREAK[workspace_id] = _AUTO_SCAN_STREAK.get(workspace_id, 0) + 1
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
    owned_merchant_ids, owned_merchant_names = _owned_shop_identities(db)
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
        "scan_interval_seconds": _policy_interval_seconds(policy),
        "pricing_mode": policy.pricing_mode,
        "delivery_price_premium_kzt": policy.delivery_price_premium_kzt,
        "delivery_advantage_days": policy.delivery_advantage_days,
        "owned_price_band_kzt": policy.owned_price_band_kzt,
        "owned_merchant_ids": owned_merchant_ids,
        "owned_merchant_names": owned_merchant_names,
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
    job.not_before_at = None
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
        state.next_scan_at = _next_scan(policy, now=now)
        return {"status": state.status, "queued_apply": False}

    owned_merchant_ids, owned_merchant_names = _owned_shop_identities(db)
    market = normalize_market_snapshot(
        market_payload or {},
        owned_merchant_ids=owned_merchant_ids,
        owned_merchant_names=owned_merchant_names,
    )
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
    if market.get("image_url"):
        product.image_url = market["image_url"]
    state.page_visible_price_kzt = _decimal(
        market.get("page_visible_price_kzt"), field="page_visible_price_kzt"
    )
    state.market_context_ok = bool(market.get("market_context_ok"))
    state.market_context_reason = market.get("market_context_reason")
    state.offers_json = market.get("offers") or []
    state.automation_json = {**(state.automation_json or {}), "offers_complete": market.get("offers_complete", False)}
    state.offers_count = len(state.offers_json)
    _refresh_owned_cycle_anchor(state)
    state.last_error_code = None
    state.last_error_message = None
    state.state_version += 1

    own_rows = [r for r in state.offers_json if r.get("is_own")]
    if (policy.enabled and not product.sale_enabled and not product.sale_state_overridden
            and state.market_context_ok and state.own_price_kzt is not None
            and state.own_price_kzt > 0 and len(own_rows) == 1
            and own_rows[0].get("own_match") in {"merchant_uid", "merchant_sku"}
            and _decimal(own_rows[0].get("price_kzt"), field="own_offer_price") == state.own_price_kzt
            and physical_stock_count(db, product_id=product.id) > 0):
        # Imported unavailability is an observation, not a manual prohibition.
        # Recover only from the current exact own offer plus physical FIFO.
        # The conditional UPDATE cannot overwrite a concurrent manual disable.
        db.execute(update(Product).where(
            Product.id == product.id, Product.workspace_id == workspace_id,
            Product.sale_enabled.is_(False), Product.sale_state_overridden.is_(False)
        ).values(sale_enabled=True))
        db.refresh(product)

    if not policy.enabled or not product.sale_enabled:
        reason = (
            "Быстрый демпинг выключен."
            if not policy.enabled
            else ("Товар вручную снят с продажи." if product.sale_state_overridden
                  else "Статус «снят с продажи» из импорта не подтверждён: нужны точный собственный оффер Kaspi и физический остаток.")
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

    decision = _decide_policy_price(db=db, policy=policy, state=state, source=source,
        own_price_kzt=state.own_price_kzt,
        competitor_price_kzt=state.competitor_price_kzt,
        safe_floor_kzt=floor,
        undercut_step_kzt=Decimal(policy.undercut_step_kzt),
        allow_price_raise=policy.allow_price_raise,
        max_undercut_gap_percent=Decimal(policy.max_undercut_gap_percent),
        market_offers=state.offers_json,
        delivery_price_premium_kzt=policy.delivery_price_premium_kzt,
        delivery_advantage_days=policy.delivery_advantage_days,
        page_visible_price_kzt=state.page_visible_price_kzt,
        owned_price_band_kzt=policy.owned_price_band_kzt,
        owned_cycle_anchor_price_kzt=state.owned_cycle_anchor_price_kzt,
    )
    delivery_selection_reason = market.get("delivery_selection_reason")
    if delivery_selection_reason:
        decision = replace(
            decision,
            status=(
                "delivery_advantage"
                if decision.status == "no_competitor"
                else decision.status
            ),
            reason=f"{decision.reason} {delivery_selection_reason}",
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

    write_allowed_at = _next_write_allowed_at(state, policy)
    if write_allowed_at is not None and now < write_allowed_at:
        _finish_without_write(
            state=state,
            job=job,
            policy=policy,
            status="cooldown",
            reason=(
                "Новая цена рассчитана, но запись разрешена не чаще выбранного "
                "интервала. CRM подождёт следующую проверку."
            ),
            now=now,
        )
        state.next_scan_at = min(write_allowed_at, _next_scan(policy, now=now)) if policy.pricing_mode == "automation" else write_allowed_at
        return {
            "status": state.status,
            "queued_apply": False,
            "decision": decision_json,
            "write_allowed_at": write_allowed_at,
        }

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
    cooldown_until = _next_write_allowed_at(state, policy)
    if state.active_job_id != job.id or state.state_version != job.state_version:
        stale_reason = "Решение уже заменено новой версией."
    elif not policy.enabled or not product.sale_enabled:
        stale_reason = "Товар или быстрый демпинг выключен."
    elif state.automatic_writes_paused:
        stale_reason = state.pause_reason or "Автозапись приостановлена."
    elif cooldown_until is not None and now < cooldown_until:
        job.status = "cooldown"
        job.completed_at = now
        job.lease_until = None
        job.lease_token = None
        _clear_active_job(state, job)
        state.status = "cooldown"
        state.status_reason = (
            "Повторная запись цены отложена до окончания выбранного интервала."
        )
        state.next_scan_at = min(cooldown_until, _next_scan(policy, now=now)) if policy.pricing_mode == "automation" else cooldown_until
        return {
            "ready": False,
            "cooldown": True,
            "reason": state.status_reason,
            "write_allowed_at": cooldown_until,
        }

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
        market = job.market_json or {}
        decision = _decide_policy_price(db=db, policy=policy, state=state, source=source,
            own_price_kzt=_decimal(
                market.get("own_price_kzt"), field="own_price_kzt"
            ),
            competitor_price_kzt=_decimal(
                market.get("competitor_price_kzt"), field="competitor_price_kzt"
            ),
            safe_floor_kzt=floor,
            undercut_step_kzt=Decimal(policy.undercut_step_kzt),
            allow_price_raise=policy.allow_price_raise,
            max_undercut_gap_percent=Decimal(policy.max_undercut_gap_percent),
            market_offers=market.get("offers") or [],
            delivery_price_premium_kzt=policy.delivery_price_premium_kzt,
            delivery_advantage_days=policy.delivery_advantage_days,
            page_visible_price_kzt=_decimal(
                market.get("page_visible_price_kzt"), field="page_visible_price_kzt"
            ),
            owned_price_band_kzt=policy.owned_price_band_kzt,
            owned_cycle_anchor_price_kzt=state.owned_cycle_anchor_price_kzt,
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
    state.last_operation_id = operation_id
    state.last_agent_id = agent_id

    if verified:
        job.status = "applied"
        job.completed_at = now
        job.not_before_at = None
        _clear_active_job(state, job)
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

    if accepted:
        verify_at = _next_scan(policy, now=now)
        job.status = "queued_verify"
        job.agent_id = None
        job.completed_at = None
        job.not_before_at = verify_at
        job.error_code = None
        job.error_message = None
        state.status = "verifying"
        state.status_reason = (
            "Kaspi принял запись. CRM выполнит одну контрольную проверку цены "
            "после выбранного интервала без частого polling."
        )
        state.last_applied_at = now
        state.last_error_code = None
        state.last_error_message = None
        state.next_scan_at = verify_at
        return {
            "status": state.status,
            "verified": False,
            "verification_scheduled": True,
            "verify_at": verify_at,
        }

    error_code = safe_write["error_code"] or "apply_failed"
    error_message = safe_write["error_message"] or "Kaspi отклонил realtime-запись."
    job.status = error_code
    job.completed_at = now
    job.not_before_at = None
    job.error_code = error_code
    job.error_message = error_message
    _clear_active_job(state, job)
    state.status = error_code
    state.status_reason = error_message
    state.last_error_code = error_code
    state.last_error_message = error_message
    state.next_scan_at = _next_scan(policy, now=now)
    return {"status": state.status, "verified": False}


def complete_verification(
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
    job.not_before_at = None
    _clear_active_job(state, job)
    if verification_succeeded and target is not None and observed == target:
        job.status = "applied"
        state.status = (
            "floor_limited"
            if (job.decision_json or {}).get("status") == "floor_limited"
            else "applied"
        )
        state.status_reason = "Цена подтверждена одной отложенной проверкой без повтора записи."
        state.last_applied_at = state.last_applied_at or now
        state.last_error_code = None
        state.last_error_message = None
        state.automatic_writes_paused = False
        state.pause_reason = None
        state.next_scan_at = _next_scan(policy, now=now)
        return {"status": state.status, "verified": True}

    if verification_succeeded and observed is not None:
        job.status = "verification_missed"
        job.error_code = "verification_missed"
        job.error_message = (
            f"Ожидалась цена {_json_money(target)} ₸, но Kaspi показал "
            f"{_json_money(observed)} ₸."
        )
        state.own_price_kzt = observed
    else:
        job.status = "verification_failed"
        job.error_code = _text(error_code, limit=128) or "verification_failed"
        job.error_message = (
            _text(error_message, limit=2000)
            or "Контрольная проверка не смогла прочитать нашу цену."
        )
    state.status = "verification_retry"
    state.status_reason = (
        "Целевая цена пока не подтверждена. Старая операция не повторяется; "
        "после выбранного интервала будет выполнен новый полный scan."
    )
    state.last_error_code = job.error_code
    state.last_error_message = job.error_message
    state.automatic_writes_paused = False
    state.pause_reason = None
    state.next_scan_at = _next_scan(policy, now=now)
    return {
        "status": state.status,
        "verified": False,
        "retry_at": state.next_scan_at,
    }


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


def _decide_policy_price(*, db, policy, state, source, **kwargs):
    if getattr(policy, "pricing_mode", "manual") != "automation":
        return decide_fast_price(**kwargs)
    from .full_automation_service import observe_sales
    from .full_automation_pricing import decide_automated_price
    if _aware(state.last_scanned_at) is None or utcnow() - _aware(state.last_scanned_at) > timedelta(seconds=300):
        return FastPriceDecision(Decimal(kwargs["safe_floor_kzt"]), None, state.own_price_kzt, state.own_price_kzt,
                                 Decimal(1), "automation_market_stale", "Рынок устарел; нужна новая проверка перед записью.", False)
    experiment = observe_sales(db, policy=policy, state=state)
    from .full_automation_owned_cycle import read_shared_cycle
    owned_cycle = read_shared_cycle(db, policy=policy, state=state)
    decision = decide_automated_price(
        own_price_kzt=kwargs["own_price_kzt"], safe_floor_kzt=kwargs["safe_floor_kzt"],
        market_offers=kwargs["market_offers"], unit_cost_kzt=source.unit_cost_kzt,
        config=policy.automation_config, offers_complete=(getattr(state, "automation_json", None) or {}).get("offers_complete", False),
        target_position=experiment["target_position"], owned_cycle=owned_cycle,
        observed_at=_aware(state.last_scanned_at),
    )
    state.automation_json = {**(state.automation_json or {}), "owned_cycle":owned_cycle, "plan_reason":decision.reason,
                             "planned_price_kzt":str(decision.target_price_kzt), "plan_status":decision.status}
    return decision
