from __future__ import annotations

from typing import Any

from .db import SessionLocal


def state_for_product(product_id: int) -> dict[str, Any] | None:
    from .kaspi_competitor_agent_api import state_for_product as read_state

    with SessionLocal() as db:
        return read_state(db, product_id)


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


async def start_dumping_competitor_worker() -> None:
    """Server worker intentionally disabled; local agent owns Kaspi scanning."""
    return None


async def stop_dumping_competitor_worker() -> None:
    return None
