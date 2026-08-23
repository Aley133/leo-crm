from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import SessionLocal
from .marketplace_import import RAW_PAYLOAD_HISTORY_PER_ORDER, prune_order_raw_payload_history
from .models import MarketplaceRawPayload
from .monitoring import MonitorAttempt

DEFAULT_CLEANUP_INTERVAL_SECONDS = 86_400
DEFAULT_STARTUP_DELAY_SECONDS = 600
DEFAULT_BROWSER_JOB_RETENTION_DAYS = 7
DEFAULT_MONITOR_ATTEMPT_RETENTION_DAYS = 14
DEFAULT_MIN_HISTORY_PER_TARGET = 20
DEFAULT_BATCH_SIZE = 5_000
DEFAULT_MAX_ROWS_PER_TABLE = 50_000
RETENTION_ADVISORY_LOCK_KEY = 1_279_545_679

LAST_RUN: dict[str, object] = {
    "status": "not_started",
    "started_at": None,
    "finished_at": None,
    "raw_payloads_removed": 0,
    "browser_agent_jobs_removed": 0,
    "monitor_attempts_removed": 0,
    "error": None,
}


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    raw_payloads_removed: int = 0
    browser_agent_jobs_removed: int = 0
    monitor_attempts_removed: int = 0
    skipped: bool = False


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def cleanup_enabled() -> bool:
    return os.getenv("DATA_RETENTION_ENABLED", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _acquire_cleanup_lock(db: Session) -> bool:
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": RETENTION_ADVISORY_LOCK_KEY},
        )
    )


def prune_global_order_raw_payloads(
    db: Session,
    *,
    keep: int = RAW_PAYLOAD_HISTORY_PER_ORDER,
) -> int:
    """Keep bounded raw audit snapshots without touching canonical orders."""

    keep = max(5, int(keep))
    oversized_orders = db.execute(
        select(
            MarketplaceRawPayload.marketplace_account_id,
            MarketplaceRawPayload.external_object_id,
        )
        .where(MarketplaceRawPayload.payload_type == "order")
        .group_by(
            MarketplaceRawPayload.marketplace_account_id,
            MarketplaceRawPayload.external_object_id,
        )
        .having(func.count(MarketplaceRawPayload.id) > keep)
    ).all()
    removed = 0
    for marketplace_account_id, external_order_id in oversized_orders:
        removed += prune_order_raw_payload_history(
            db,
            marketplace_account_id=int(marketplace_account_id),
            external_order_id=str(external_order_id),
            keep=keep,
        )
    return removed


def _prune_browser_agent_jobs_batch(
    db: Session,
    *,
    cutoff: datetime,
    min_history_per_target: int,
    batch_size: int,
) -> int:
    ranked = (
        select(
            BrowserAgentJob.id.label("id"),
            BrowserAgentJob.finished_at.label("finished_at"),
            func.row_number()
            .over(
                partition_by=(
                    BrowserAgentJob.monitor_target_id,
                    BrowserAgentJob.supplier_product_id,
                ),
                order_by=(
                    BrowserAgentJob.finished_at.desc(),
                    BrowserAgentJob.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(
            BrowserAgentJob.status.in_(
                (BrowserAgentJobStatus.SUCCEEDED.value, BrowserAgentJobStatus.FAILED.value)
            ),
            BrowserAgentJob.finished_at.is_not(None),
        )
        .subquery()
    )
    candidate_ids = list(
        db.scalars(
            select(ranked.c.id)
            .where(
                ranked.c.finished_at < cutoff,
                ranked.c.rn > max(1, int(min_history_per_target)),
            )
            .order_by(ranked.c.id)
            .limit(max(1, int(batch_size)))
        ).all()
    )
    if not candidate_ids:
        return 0
    result = db.execute(delete(BrowserAgentJob).where(BrowserAgentJob.id.in_(candidate_ids)))
    return int(result.rowcount or 0)


def prune_browser_agent_history(
    db: Session,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_BROWSER_JOB_RETENTION_DAYS,
    min_history_per_target: int = DEFAULT_MIN_HISTORY_PER_TARGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int = DEFAULT_MAX_ROWS_PER_TABLE,
) -> int:
    """Prune old completed jobs while preserving active queue rows and diagnostics."""

    cutoff = (now or datetime.now(UTC)) - timedelta(days=max(1, int(retention_days)))
    removed = 0
    while removed < max(1, int(max_rows)):
        current_batch = _prune_browser_agent_jobs_batch(
            db,
            cutoff=cutoff,
            min_history_per_target=min_history_per_target,
            batch_size=min(batch_size, max_rows - removed),
        )
        removed += current_batch
        if current_batch == 0:
            break
    return removed


def _prune_monitor_attempts_batch(
    db: Session,
    *,
    cutoff: datetime,
    min_history_per_target: int,
    batch_size: int,
) -> int:
    ranked = (
        select(
            MonitorAttempt.id.label("id"),
            MonitorAttempt.finished_at.label("finished_at"),
            func.row_number()
            .over(
                partition_by=MonitorAttempt.monitor_target_id,
                order_by=(MonitorAttempt.finished_at.desc(), MonitorAttempt.id.desc()),
            )
            .label("rn"),
        )
        .where(MonitorAttempt.finished_at.is_not(None))
        .subquery()
    )
    candidate_ids = list(
        db.scalars(
            select(ranked.c.id)
            .where(
                ranked.c.finished_at < cutoff,
                ranked.c.rn > max(1, int(min_history_per_target)),
            )
            .order_by(ranked.c.id)
            .limit(max(1, int(batch_size)))
        ).all()
    )
    if not candidate_ids:
        return 0
    result = db.execute(delete(MonitorAttempt).where(MonitorAttempt.id.in_(candidate_ids)))
    return int(result.rowcount or 0)


def prune_monitor_attempt_history(
    db: Session,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_MONITOR_ATTEMPT_RETENTION_DAYS,
    min_history_per_target: int = DEFAULT_MIN_HISTORY_PER_TARGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int = DEFAULT_MAX_ROWS_PER_TABLE,
) -> int:
    """Bound execution logs; current supplier state and observations are untouched."""

    cutoff = (now or datetime.now(UTC)) - timedelta(days=max(1, int(retention_days)))
    removed = 0
    while removed < max(1, int(max_rows)):
        current_batch = _prune_monitor_attempts_batch(
            db,
            cutoff=cutoff,
            min_history_per_target=min_history_per_target,
            batch_size=min(batch_size, max_rows - removed),
        )
        removed += current_batch
        if current_batch == 0:
            break
    return removed


def run_retention_cleanup(*, now: datetime | None = None) -> RetentionCleanupResult:
    """Run one bounded cleanup transaction across all workspaces."""

    if not cleanup_enabled():
        return RetentionCleanupResult(skipped=True)

    browser_days = _env_int(
        "BROWSER_JOB_RETENTION_DAYS",
        DEFAULT_BROWSER_JOB_RETENTION_DAYS,
        minimum=1,
        maximum=90,
    )
    monitor_days = _env_int(
        "MONITOR_ATTEMPT_RETENTION_DAYS",
        DEFAULT_MONITOR_ATTEMPT_RETENTION_DAYS,
        minimum=1,
        maximum=180,
    )
    min_history = _env_int(
        "TECHNICAL_HISTORY_PER_TARGET",
        DEFAULT_MIN_HISTORY_PER_TARGET,
        minimum=5,
        maximum=200,
    )
    max_rows = _env_int(
        "RETENTION_MAX_ROWS_PER_TABLE",
        DEFAULT_MAX_ROWS_PER_TABLE,
        minimum=1_000,
        maximum=250_000,
    )

    with SessionLocal() as db:
        # Retention is infrastructure maintenance, not a workspace request. Without
        # this flag the ORM safety hook would silently scope deletes to workspace 1.
        db.info["include_all_workspaces"] = True
        if not _acquire_cleanup_lock(db):
            db.rollback()
            return RetentionCleanupResult(skipped=True)
        result = RetentionCleanupResult(
            raw_payloads_removed=prune_global_order_raw_payloads(db),
            browser_agent_jobs_removed=prune_browser_agent_history(
                db,
                now=now,
                retention_days=browser_days,
                min_history_per_target=min_history,
                max_rows=max_rows,
            ),
            monitor_attempts_removed=prune_monitor_attempt_history(
                db,
                now=now,
                retention_days=monitor_days,
                min_history_per_target=min_history,
                max_rows=max_rows,
            ),
        )
        db.commit()
        return result


async def _wait_or_stop(stop_event: asyncio.Event, seconds: int) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(1, int(seconds)))
        return True
    except TimeoutError:
        return False


async def retention_cleanup_loop(stop_event: asyncio.Event) -> None:
    """Run retention daily in a worker thread so API requests are never blocked."""

    if not cleanup_enabled():
        LAST_RUN.update(status="disabled", error=None)
        return

    startup_delay = _env_int(
        "DATA_RETENTION_STARTUP_DELAY_SECONDS",
        DEFAULT_STARTUP_DELAY_SECONDS,
        minimum=30,
        maximum=86_400,
    )
    interval = _env_int(
        "DATA_RETENTION_INTERVAL_SECONDS",
        DEFAULT_CLEANUP_INTERVAL_SECONDS,
        minimum=3_600,
        maximum=604_800,
    )
    if await _wait_or_stop(stop_event, startup_delay):
        return

    while not stop_event.is_set():
        started_at = datetime.now(UTC)
        LAST_RUN.update(
            status="running", started_at=started_at.isoformat(), finished_at=None, error=None
        )
        try:
            result = await asyncio.to_thread(run_retention_cleanup, now=started_at)
            LAST_RUN.update(
                status="skipped" if result.skipped else "ok",
                finished_at=datetime.now(UTC).isoformat(),
                **{key: value for key, value in asdict(result).items() if key != "skipped"},
                error=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            LAST_RUN.update(
                status="error",
                finished_at=datetime.now(UTC).isoformat(),
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        if await _wait_or_stop(stop_event, interval):
            return
