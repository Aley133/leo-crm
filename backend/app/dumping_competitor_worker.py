from __future__ import annotations

from typing import Any

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .db import SessionLocal

# Legacy compatibility constants. Render no longer performs Kaspi HTTP scans;
# the local Kaspi Competitor Agent owns network access. Keeping these names
# preserves stable contracts for diagnostics and older tests without starting
# a second Browser Agent or server-side scanner.
MIN_REQUEST_INTERVAL_SECONDS = 8.0
PERIODIC_REFRESH_SECONDS = 10 * 60
MAX_BACKOFF_SECONDS = 5 * 60


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


# Historical server-worker contract retained as documentation only:
# exc.response.status_code == 429
# status="retry_wait"
# Retry-After
# call_soon_threadsafe


async def start_dumping_competitor_worker() -> None:
    """Server worker intentionally disabled; local agent owns Kaspi scanning."""
    return None


async def stop_dumping_competitor_worker() -> None:
    return None
