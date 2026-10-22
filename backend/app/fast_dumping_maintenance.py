"""Bounded queue maintenance outside Agent HTTP requests; jobs stay durable in DB."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from .db import SessionLocal, engine
from .fast_dumping_diagnostics import trace_fast_operation
from .fast_dumping_models import FastDumpingPolicy
from .fast_dumping_service import maintain_workspace_queue
from .fast_dumping_uow import fast_unit_of_work
from .workspace_context import workspace_context
from .workspace_models import Workspace

_LOGGER = logging.getLogger("uvicorn.error")
INTERVAL_SECONDS = 10
STATUS = {"status": "idle", "finished_at": None, "workspaces": 0}
_LAST_WORKSPACE = 0


def _pool_has_room() -> bool:
    pool = engine.pool
    return not (hasattr(pool, "checkedout") and hasattr(pool, "size")
                and pool.checkedout() >= max(1, pool.size() - 1))


def run_maintenance_cycle() -> None:
    global _LAST_WORKSPACE
    if not _pool_has_room():
        STATUS.update(status="deferred_pool_busy")
        return
    with SessionLocal() as db:
        db.info["include_all_workspaces"] = True
        workspace_ids = db.scalars(select(FastDumpingPolicy.workspace_id)
            .join(Workspace, Workspace.id == FastDumpingPolicy.workspace_id)
            .where(Workspace.is_active.is_(True)).distinct().order_by(FastDumpingPolicy.workspace_id)).all()
    workspace_ids = ([w for w in workspace_ids if w > _LAST_WORKSPACE]
                     + [w for w in workspace_ids if w <= _LAST_WORKSPACE])
    completed = 0
    failures = 0
    for workspace_id in workspace_ids:
        if not _pool_has_room():
            break
        _LAST_WORKSPACE = workspace_id
        try:
            with SessionLocal() as db, workspace_context(workspace_id), fast_unit_of_work(db, workspace_id):
                if db.bind.dialect.name == "postgresql":
                    # Avoid two rollout instances doing the same maintenance.
                    if not db.scalar(select(func.pg_try_advisory_xact_lock(172901, workspace_id))):
                        continue
                    db.execute(text("SET LOCAL lock_timeout = '1s'"))
                    db.execute(text("SET LOCAL statement_timeout = '5s'"))
                with trace_fast_operation("queue_maintenance", workspace_id) as trace:
                    maintain_workspace_queue(db, workspace_id=workspace_id)
                    db.commit()
                    trace.outcome = "completed"
                completed += 1
        except Exception as exc:
            failures += 1
            # Never log SQL parameters, credentials, payloads or lease values.
            _LOGGER.warning("Fast queue maintenance workspace=%s failed=%s", workspace_id, type(exc).__name__)
    STATUS.update(status="retry_pending" if failures else "completed",
                  workspaces=completed, failed_workspaces=failures,
                  finished_at=datetime.now(UTC).isoformat())


async def maintenance_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_maintenance_cycle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATUS.update(status="retry_pending", error_type=type(exc).__name__)
            _LOGGER.warning("Fast queue maintenance discovery failed=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
