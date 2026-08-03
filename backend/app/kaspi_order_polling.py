from __future__ import annotations

import asyncio
import os
from time import monotonic
from datetime import UTC, datetime
from typing import Any

from .kaspi_active_order_reconciliation import reconcile_active_orders
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
from .db import SessionLocal
from .workspace_context import workspace_context
from .workspace_kaspi import (
    WorkspaceKaspiConnection,
    list_workspace_kaspi_connections,
)


POLL_INTERVAL_SECONDS = 60
STARTUP_DELAY_SECONDS = 30
FAST_LOOKBACK_MINUTES = 20
FULL_REFRESH_EVERY = 10
DEEP_REFRESH_EVERY = 60
FAST_ORDER_STATES = (
    "NEW",
    "SIGN_REQUIRED",
    "PICKUP",
    "DELIVERY",
    "KASPI_DELIVERY",
)
LAST_RUN: dict[str, Any] = {
    "status": "idle",
    "cycle": 0,
    "started_at": None,
    "finished_at": None,
    "days": None,
    "mode": None,
    "lookback_minutes": None,
    "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    "raw_job_id": None,
    "enrichment_job_id": None,
    "message": "Kaspi polling has not started",
}


def polling_enabled() -> bool:
    raw = os.getenv("KASPI_ORDER_POLL_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def polling_interval_seconds() -> int:
    raw = os.getenv("KASPI_ORDER_POLL_INTERVAL_SECONDS", str(POLL_INTERVAL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return POLL_INTERVAL_SECONDS
    return max(30, value)


def polling_startup_delay_seconds() -> float:
    raw = os.getenv(
        "KASPI_ORDER_POLL_STARTUP_DELAY_SECONDS",
        str(STARTUP_DELAY_SECONDS),
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return float(STARTUP_DELAY_SECONDS)
    return max(0.0, min(300.0, value))


async def _run_account_cycle(
    connection: WorkspaceKaspiConnection,
    *,
    days: int,
    mode: str,
    lookback_minutes: int | None,
    enrich_products: bool,
) -> dict[str, Any]:
    with workspace_context(connection.workspace_id):
        raw_job_id = create_raw_job(
            days=days,
            timezone_name=connection.timezone,
            marketplace_account_id=connection.account_id,
            workspace_id=connection.workspace_id,
            lookback_minutes=lookback_minutes,
            states=FAST_ORDER_STATES if mode == "fast" else None,
        )
        await run_raw_job(
            raw_job_id,
            api_token=connection.api_token,
            marketplace_account_id=connection.account_id,
        )
        raw_job = RAW_JOBS.get(raw_job_id) or {}
        if raw_job.get("status") == "failed":
            return {
                "workspace_id": connection.workspace_id,
                "account_id": connection.account_id,
                "status": "failed",
                "raw_job_id": raw_job_id,
                "message": str(raw_job.get("message") or "Kaspi raw import failed"),
            }

        enrichment_job_id = None
        enrichment: dict[str, Any] = {}
        if enrich_products:
            enrichment_job_id = create_enrichment_job(
                days=days,
                marketplace_account_id=connection.account_id,
                workspace_id=connection.workspace_id,
                lookback_minutes=lookback_minutes if mode == "fast" else None,
            )
            await run_enrichment_job(
                enrichment_job_id,
                api_token=connection.api_token,
                marketplace_account_id=connection.account_id,
            )
            enrichment = public_enrichment_job(enrichment_job_id) or {}
        active_reconciliation = None
        if mode != "fast":
            active_reconciliation = await reconcile_active_orders(connection)
        raw_errors = list(raw_job.get("errors") or [])
        enrichment_errors = list(enrichment.get("errors") or [])
        reconciliation_errors = (
            list(active_reconciliation.errors)
            if active_reconciliation is not None
            else []
        )
        has_errors = bool(raw_errors or enrichment_errors or reconciliation_errors)
        return {
            "workspace_id": connection.workspace_id,
            "account_id": connection.account_id,
            "mode": mode,
            "status": "completed_with_errors" if has_errors else "completed",
            "raw_job_id": raw_job_id,
            "enrichment_job_id": enrichment_job_id,
            "orders": raw_job.get("orders_count", 0),
            "imported": raw_job.get("imported_count", 0),
            "updated": raw_job.get("updated_count", 0),
            "product_lines": enrichment.get("updated", 0),
            "linked": enrichment.get("linked", 0),
            "allocated": enrichment.get("allocated", 0),
            "active_orders_checked": (
                active_reconciliation.checked
                if active_reconciliation is not None
                else 0
            ),
            "active_orders_found": (
                active_reconciliation.found
                if active_reconciliation is not None
                else 0
            ),
            "active_orders_updated": (
                active_reconciliation.updated
                if active_reconciliation is not None
                else 0
            ),
            "active_orders_missing": (
                active_reconciliation.missing
                if active_reconciliation is not None
                else 0
            ),
            "errors": (
                len(raw_errors)
                + len(enrichment_errors)
                + len(reconciliation_errors)
            ),
        }


async def run_poll_cycle(
    *,
    days: int,
    mode: str = "full",
    lookback_minutes: int | None = None,
    enrich_products: bool = True,
) -> None:
    LAST_RUN.update(
        {
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "days": days,
            "mode": mode,
            "lookback_minutes": lookback_minutes,
            "poll_interval_seconds": polling_interval_seconds(),
            "message": (
                f"Kaspi order polling started: mode={mode}, "
                f"lookback={lookback_minutes or days * 24 * 60} minute(s)"
            ),
        }
    )

    with SessionLocal() as session:
        connections = list_workspace_kaspi_connections(session)
    if not connections:
        LAST_RUN.update(
            {
                "status": "not_configured",
                "finished_at": datetime.now(UTC).isoformat(),
                "message": "No active Kaspi accounts are configured",
                "accounts": [],
            }
        )
        return

    results: list[dict[str, Any]] = []
    for connection in connections:
        results.append(
            await _run_account_cycle(
                connection,
                days=days,
                mode=mode,
                lookback_minutes=lookback_minutes,
                enrich_products=enrich_products,
            )
        )
    has_failures = any(item["status"] == "failed" for item in results)
    has_errors = any(item["status"] == "completed_with_errors" for item in results)
    totals = {
        key: sum(int(item.get(key) or 0) for item in results)
        for key in (
            "orders",
            "imported",
            "updated",
            "product_lines",
            "linked",
            "allocated",
            "active_orders_checked",
            "active_orders_found",
            "active_orders_updated",
            "active_orders_missing",
            "errors",
        )
    }
    LAST_RUN.update(
        {
            "status": (
                "failed"
                if has_failures
                else "completed_with_errors"
                if has_errors
                else "completed"
            ),
            "finished_at": datetime.now(UTC).isoformat(),
            "accounts": results,
            "message": (
                f"Kaspi polling {mode} completed for {len(results)} account(s): "
                f"orders={totals['orders']}, imported={totals['imported']}, "
                f"updated={totals['updated']}, product_lines={totals['product_lines']}, "
                f"linked={totals['linked']}, allocated={totals['allocated']}, "
                f"active_checked={totals['active_orders_checked']}, "
                f"active_updated={totals['active_orders_updated']}, "
                f"errors={totals['errors']}"
            ),
        }
    )


async def polling_loop(stop_event: asyncio.Event) -> None:
    if not polling_enabled():
        LAST_RUN["message"] = "Kaspi polling is disabled"
        return

    startup_delay = polling_startup_delay_seconds()
    if startup_delay:
        LAST_RUN["message"] = (
            f"Kaspi polling will start after {startup_delay:g} second startup grace period"
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=startup_delay)
            return
        except TimeoutError:
            pass

    cycle = 0
    while not stop_event.is_set():
        cycle_started = monotonic()
        cycle += 1
        LAST_RUN["cycle"] = cycle
        try:
            if cycle % DEEP_REFRESH_EVERY == 0:
                await run_poll_cycle(days=7, mode="deep", enrich_products=True)
            elif cycle % FULL_REFRESH_EVERY == 0:
                await run_poll_cycle(days=1, mode="full", enrich_products=True)
            else:
                await run_poll_cycle(
                    days=1,
                    mode="fast",
                    lookback_minutes=FAST_LOOKBACK_MINUTES,
                    enrich_products=True,
                )
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

        elapsed = monotonic() - cycle_started
        wait_seconds = max(0.1, polling_interval_seconds() - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
        except TimeoutError:
            continue
