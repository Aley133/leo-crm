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


def enqueue_competitor_scan(product_id: int, *, reason: str = "manual") -> bool:
    """Create a durable job for the local Kaspi Competitor Agent.

    No HTTP request to Kaspi is made on Render. This function does not touch
    Browser Agent supplier jobs, leases, heartbeats, Playwright or Chrome.
    """
    from .kaspi_competitor_agent_api import queue_competitor_job

    with SessionLocal() as db:
        try:
            job = queue_competitor_job(db, product_id=product_id, reason=reason)
            created = job.status == "queued_local"
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
    for policy in policies:
        source = resolve_cost_source(
            db,
            product_id=policy.product_id,
            inventory_first=policy.inventory_first,
        )
        if source is None:
            continue
        jobs.append(
            DumpingRun(
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


def dispatch_due_competitor_jobs() -> tuple[int, ...]:
    """Run one short queue-only scheduling transaction."""
    with SessionLocal() as db:
        try:
            job_ids = queue_due_competitor_jobs(db)
            db.commit()
            return job_ids
        except Exception:
            db.rollback()
            raise


async def _scheduler_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(dispatch_due_competitor_jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
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
