from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from .kaspi_product_enrichment_jobs import (
    create_job as create_enrichment_job,
    public_job as public_enrichment_job,
    run_job as run_enrichment_job,
)
from .kaspi_raw_receiver_jobs import (
    JOBS as RAW_JOBS,
    create_job as create_raw_job,
    run_job as run_raw_job,
)


POLL_INTERVAL_SECONDS = 600
FULL_REFRESH_EVERY = 6
LAST_RUN: dict[str, Any] = {
    "status": "idle",
    "cycle": 0,
    "started_at": None,
    "finished_at": None,
    "days": None,
    "raw_job_id": None,
    "enrichment_job_id": None,
    "message": "Kaspi polling has not started",
}


def polling_enabled() -> bool:
    raw = os.getenv("KASPI_ORDER_POLL_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"} and bool(
        os.getenv("KASPI_API_TOKEN", "").strip()
    )


def polling_interval_seconds() -> int:
    raw = os.getenv("KASPI_ORDER_POLL_INTERVAL_SECONDS", str(POLL_INTERVAL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return POLL_INTERVAL_SECONDS
    return max(60, value)


async def run_poll_cycle(*, days: int) -> None:
    LAST_RUN.update(
        {
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "days": days,
            "message": f"Kaspi order polling started for {days} day(s)",
        }
    )

    raw_job_id = create_raw_job(days=days)
    LAST_RUN["raw_job_id"] = raw_job_id
    await run_raw_job(raw_job_id)
    raw_job = RAW_JOBS.get(raw_job_id) or {}
    if raw_job.get("status") == "failed":
        LAST_RUN.update(
            {
                "status": "failed",
                "finished_at": datetime.now(UTC).isoformat(),
                "message": str(raw_job.get("message") or "Kaspi raw import failed"),
            }
        )
        return

    enrichment_job_id = create_enrichment_job(days=days)
    LAST_RUN["enrichment_job_id"] = enrichment_job_id
    await run_enrichment_job(enrichment_job_id)
    enrichment = public_enrichment_job(enrichment_job_id) or {}

    raw_errors = list(raw_job.get("errors") or [])
    enrichment_errors = list(enrichment.get("errors") or [])
    has_errors = bool(raw_errors or enrichment_errors)
    LAST_RUN.update(
        {
            "status": "completed_with_errors" if has_errors else "completed",
            "finished_at": datetime.now(UTC).isoformat(),
            "message": (
                f"Kaspi polling completed: orders={raw_job.get('orders_count', 0)}, "
                f"imported={raw_job.get('imported_count', 0)}, "
                f"updated={raw_job.get('updated_count', 0)}, "
                f"product_lines={enrichment.get('updated', 0)}, "
                f"linked={enrichment.get('linked', 0)}, "
                f"allocated={enrichment.get('allocated', 0)}, "
                f"errors={len(raw_errors) + len(enrichment_errors)}"
            ),
        }
    )


async def polling_loop(stop_event: asyncio.Event) -> None:
    if not polling_enabled():
        LAST_RUN["message"] = "Kaspi polling disabled or KASPI_API_TOKEN is missing"
        return

    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        LAST_RUN["cycle"] = cycle
        days = 7 if cycle % FULL_REFRESH_EVERY == 0 else 1
        try:
            await run_poll_cycle(days=days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LAST_RUN.update(
                {
                    "status": "failed",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "message": f"Kaspi polling failed: {type(exc).__name__}: {exc}",
                }
            )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=polling_interval_seconds())
        except TimeoutError:
            continue
