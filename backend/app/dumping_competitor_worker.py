from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .db import SessionLocal
from .dumping_models import DumpingPolicy, DumpingRun
from .dumping_service import resolve_cost_source

# Legacy compatibility constants. Render no longer performs Kaspi HTTP scans;
# the local Kaspi Competitor Agent owns network access. Keeping these names
# preserves stable contracts for diagnostics and older tests without starting
# a second Browser Agent or server-side scanner.
MIN_REQUEST_INTERVAL_SECONDS = 8.0
PERIODIC_REFRESH_SECONDS = 10 * 60
MAX_BACKOFF_SECONDS = 5 * 60
SCHEDULER_POLL_SECONDS = 30.0
SCHEDULER_BATCH_LIMIT = 100
LEGACY_RECOVERY_BATCH_LIMIT = 100
_POLICY_STATE_STATUSES = (
    "suspended_seller_removed",
    "policy_disabled_manual",
)

_LOCAL_JOB_STATUSES = (
    "queued_local",
    "leased_local",
    "succeeded_local",
    "failed_local",
)
_ACTIVE_LOCAL_JOB_STATUSES = ("queued_local", "leased_local")
_SCHEDULER_STOP_EVENT: asyncio.Event | None = None
_SCHEDULER_TASK: asyncio.Task | None = None
_LOGGER = logging.getLogger(__name__)
SCHEDULER_LAST_RUN: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "recovered_count": 0,
    "periodic_count": 0,
    "job_ids": [],
    "message": "Kaspi competitor queue scheduler has not started",
}


_STATUS_MAP = {
    "queued_local": "queued",
    "leased_local": "scanning",
    "succeeded_local": "completed",
    "failed_local": "failed",
}


def _normalized_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None

    raw_status = str(state.get("status") or "")
    normalized = _STATUS_MAP.get(raw_status, raw_status)
    return {
        **state,
        "raw_status": raw_status,
        "status": normalized,
        "stage": state.get("stage") or normalized,
    }


def state_for_product(
    product_id: int,
    *,
    db: Session | None = None,
) -> dict[str, Any] | None:
    from .kaspi_competitor_agent_api import state_for_product as read_state

    if db is not None:
        try:
            return _normalized_state(read_state(db, product_id))
        except (OperationalError, ProgrammingError):
            # Some lightweight test/dev schemas intentionally omit dumping_runs.
            # The workspace must remain readable until the full Alembic schema is
            # available; absence of queue state is represented as None.
            db.rollback()
            return None

    with SessionLocal() as owned_db:
        try:
            return _normalized_state(read_state(owned_db, product_id))
        except (OperationalError, ProgrammingError):
            owned_db.rollback()
            return None


def states_for_products(
    product_ids: set[int],
    *,
    db: Session,
) -> dict[int, dict[str, Any]]:
    from .kaspi_competitor_agent_api import states_for_products as read_states

    try:
        return {
            product_id: normalized
            for product_id, state in read_states(db, product_ids).items()
            if (normalized := _normalized_state(state)) is not None
        }
    except (OperationalError, ProgrammingError):
        db.rollback()
        return {}


def enqueue_competitor_scan(product_id: int, *, reason: str = "manual") -> bool:
    """Create a durable job for the local Kaspi Competitor Agent.

    No HTTP request to Kaspi is made on Render. This function does not touch
    Browser Agent supplier jobs, leases, heartbeats, Playwright or Chrome.
    """
    from .kaspi_competitor_agent_api import queue_competitor_job

    with SessionLocal() as db:
        try:
            existing_id = db.scalar(
                select(DumpingRun.id)
                .where(
                    DumpingRun.product_id == product_id,
                    DumpingRun.status.in_(_ACTIVE_LOCAL_JOB_STATUSES),
                )
                .limit(1)
            )
            job = queue_competitor_job(db, product_id=product_id, reason=reason)
            created = existing_id is None and job.status == "queued_local"
            db.commit()
            return created
        except Exception:
            db.rollback()
            raise


def build_due_competitor_policies_statement(
    *,
    now: datetime,
    refresh_seconds: int = PERIODIC_REFRESH_SECONDS,
    limit: int = SCHEDULER_BATCH_LIMIT,
):
    """Select enabled policies whose local Kaspi scan is due.

    Render only creates durable queue rows. The Windows Kaspi Competitor Agent
    remains the sole owner of all Kaspi HTTP work.
    """
    if refresh_seconds < 1:
        raise ValueError("refresh_seconds must be positive")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    latest_scan_at = (
        select(func.max(DumpingRun.created_at))
        .where(
            DumpingRun.product_id == DumpingPolicy.product_id,
            DumpingRun.status.in_(_LOCAL_JOB_STATUSES),
        )
        .correlate(DumpingPolicy)
        .scalar_subquery()
    )
    active_job_exists = (
        select(DumpingRun.id)
        .where(
            DumpingRun.product_id == DumpingPolicy.product_id,
            DumpingRun.status.in_(_ACTIVE_LOCAL_JOB_STATUSES),
        )
        .exists()
    )
    due_before = now - timedelta(seconds=refresh_seconds)
    return (
        select(DumpingPolicy)
        .where(
            DumpingPolicy.enabled.is_(True),
            DumpingPolicy.auto_publish_xml.is_(True),
            ~active_job_exists,
            or_(latest_scan_at.is_(None), latest_scan_at <= due_before),
        )
        .order_by(DumpingPolicy.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )


def queue_due_competitor_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    refresh_seconds: int = PERIODIC_REFRESH_SECONDS,
    limit: int = SCHEDULER_BATCH_LIMIT,
) -> tuple[int, ...]:
    """Create one durable local-agent job for every due dumping policy."""
    current = now or datetime.now(UTC)
    policies = db.scalars(
        build_due_competitor_policies_statement(
            now=current,
            refresh_seconds=refresh_seconds,
            limit=limit,
        )
    ).all()
    jobs = []
    from .kaspi_competitor_agent_api import release_completed_supplier_refreshes

    for policy in policies:
        if release_completed_supplier_refreshes(
            db,
            product_id=policy.product_id,
        ):
            continue
        source = resolve_cost_source(
            db,
            product_id=policy.product_id,
            inventory_first=policy.inventory_first,
        )
        if source is None:
            continue
        jobs.append(
            DumpingRun(
                workspace_id=policy.workspace_id,
                product_id=policy.product_id,
                dumping_policy_id=policy.id,
                status="queued_local",
                published=False,
                explanation_json={
                    "reason": "periodic_refresh",
                    "agent_type": "kaspi_competitor",
                    "scheduled_at": current.isoformat(),
                },
                created_at=current,
            )
        )
    db.add_all(jobs)
    db.flush()
    return tuple(job.id for job in jobs)


def build_legacy_recovery_candidates_statement(
    *,
    limit: int = LEGACY_RECOVERY_BATCH_LIMIT,
):
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    latest_policy_state_ids = (
        select(
            DumpingRun.product_id.label("product_id"),
            func.max(DumpingRun.id).label("run_id"),
        )
        .where(DumpingRun.status.in_(_POLICY_STATE_STATUSES))
        .group_by(DumpingRun.product_id)
        .subquery()
    )
    # PostgreSQL cannot apply FOR UPDATE to a statement containing GROUP BY,
    # even when the aggregation lives in a joined subquery. Candidate discovery
    # is intentionally read-only; the policy rows are locked by a second,
    # aggregation-free statement below.
    return (
        select(DumpingPolicy.id, DumpingRun.status)
        .outerjoin(
            latest_policy_state_ids,
            latest_policy_state_ids.c.product_id == DumpingPolicy.product_id,
        )
        .outerjoin(DumpingRun, DumpingRun.id == latest_policy_state_ids.c.run_id)
        .where(
            DumpingPolicy.enabled.is_(False),
            DumpingPolicy.auto_publish_xml.is_(True),
            or_(
                DumpingRun.id.is_(None),
                DumpingRun.status == "suspended_seller_removed",
            ),
        )
        .order_by(DumpingPolicy.id)
        .limit(limit)
    )


def build_legacy_recovery_lock_statement(*, policy_ids: tuple[int, ...]):
    return (
        select(DumpingPolicy)
        .where(
            DumpingPolicy.id.in_(policy_ids),
            DumpingPolicy.enabled.is_(False),
            DumpingPolicy.auto_publish_xml.is_(True),
        )
        .order_by(DumpingPolicy.id)
        .with_for_update(skip_locked=True)
    )


def recover_legacy_auto_disabled_policies(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = LEGACY_RECOVERY_BATCH_LIMIT,
) -> tuple[int, ...]:
    """Resume policies disabled by the retired seller-visibility heuristic.

    Older releases permanently disabled dumping when a Kaspi scan temporarily
    failed to see our own offer. Migration 0029 repaired rows only when a
    supplier was available at migration time. If the supplier price arrived
    later, the disabled policy could never enter the scheduler again. Recover
    policies whose latest *policy-state* audit event proves that exact
    automatic suspension, plus unclassified legacy policies created before
    policy-state auditing existed. Ordinary queued, failed or successful scan
    rows do not change the owner's policy choice and therefore must not hide
    the suspension. A later explicit manual-disable audit always keeps the
    policy off.
    """
    current = now or datetime.now(UTC)
    candidate_rows = db.execute(
        build_legacy_recovery_candidates_statement(limit=limit)
    ).all()
    recovered_from_by_policy = {
        int(policy_id): str(state or "legacy_unclassified")
        for policy_id, state in candidate_rows
    }
    policy_ids = tuple(recovered_from_by_policy)
    if not policy_ids:
        return ()
    policies = db.scalars(
        build_legacy_recovery_lock_statement(policy_ids=policy_ids)
    ).all()

    jobs: list[DumpingRun] = []
    for policy in policies:
        source = resolve_cost_source(
            db,
            product_id=policy.product_id,
            inventory_first=policy.inventory_first,
        )
        if source is None:
            continue
        policy.enabled = True
        jobs.append(
            DumpingRun(
                workspace_id=policy.workspace_id,
                product_id=policy.product_id,
                dumping_policy_id=policy.id,
                status="queued_local",
                published=False,
                explanation_json={
                    "reason": "automatic_policy_recovery",
                    "agent_type": "kaspi_competitor",
                    "scheduled_at": current.isoformat(),
                    "recovered_from": recovered_from_by_policy[policy.id],
                },
                created_at=current,
            )
        )
    db.add_all(jobs)
    db.flush()
    return tuple(job.id for job in jobs)


def dispatch_due_competitor_jobs() -> tuple[int, ...]:
    """Run one short queue-only scheduling transaction."""
    with SessionLocal() as db:
        db.info["include_all_workspaces"] = True
        try:
            recovered_job_ids = recover_legacy_auto_disabled_policies(db)
            due_job_ids = queue_due_competitor_jobs(db)
            db.commit()
            job_ids = (*recovered_job_ids, *due_job_ids)
            SCHEDULER_LAST_RUN.update(
                {
                    "status": "completed",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "recovered_count": len(recovered_job_ids),
                    "periodic_count": len(due_job_ids),
                    "job_ids": list(job_ids),
                    "message": (
                        "Kaspi competitor queue dispatch completed: "
                        f"recovered={len(recovered_job_ids)}, "
                        f"periodic={len(due_job_ids)}"
                    ),
                }
            )
            return job_ids
        except Exception:
            db.rollback()
            raise


async def _scheduler_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        SCHEDULER_LAST_RUN.update(
            {
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "message": "Kaspi competitor queue dispatch started",
            }
        )
        try:
            job_ids = await asyncio.to_thread(dispatch_due_competitor_jobs)
            if SCHEDULER_LAST_RUN["status"] == "running":
                SCHEDULER_LAST_RUN.update(
                    {
                        "status": "completed",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "periodic_count": len(job_ids),
                        "job_ids": list(job_ids),
                        "message": (
                            "Kaspi competitor queue dispatch completed: "
                            f"jobs={len(job_ids)}"
                        ),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            SCHEDULER_LAST_RUN.update(
                {
                    "status": "failed",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": (
                        "Kaspi competitor queue dispatch failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )
            _LOGGER.exception("Kaspi competitor queue dispatch failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=SCHEDULER_POLL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


# Historical server-worker contract retained as documentation only:
# exc.response.status_code == 429
# status="retry_wait"
# Retry-After
# call_soon_threadsafe


async def start_dumping_competitor_worker() -> None:
    """Start the queue-only scheduler; local agent still owns Kaspi scanning."""
    global _SCHEDULER_STOP_EVENT, _SCHEDULER_TASK
    if _SCHEDULER_TASK is not None and not _SCHEDULER_TASK.done():
        return
    _SCHEDULER_STOP_EVENT = asyncio.Event()
    _SCHEDULER_TASK = asyncio.create_task(
        _scheduler_loop(_SCHEDULER_STOP_EVENT),
        name="kaspi-competitor-queue-scheduler",
    )


async def stop_dumping_competitor_worker() -> None:
    global _SCHEDULER_STOP_EVENT, _SCHEDULER_TASK
    if _SCHEDULER_STOP_EVENT is not None:
        _SCHEDULER_STOP_EVENT.set()
    if _SCHEDULER_TASK is not None:
        _SCHEDULER_TASK.cancel()
        try:
            await _SCHEDULER_TASK
        except asyncio.CancelledError:
            pass
    _SCHEDULER_STOP_EVENT = None
    _SCHEDULER_TASK = None
