from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.app import (
    kaspi_order_polling,
    kaspi_product_enrichment_jobs,
    kaspi_raw_receiver_jobs,
)
from backend.app.commerce import api as commerce_api


def test_fast_raw_job_reads_only_recent_active_order_window() -> None:
    kaspi_raw_receiver_jobs.JOBS.clear()
    job_id = kaspi_raw_receiver_jobs.create_job(
        days=1,
        lookback_minutes=kaspi_order_polling.FAST_LOOKBACK_MINUTES,
        states=kaspi_order_polling.FAST_ORDER_STATES,
    )
    job = kaspi_raw_receiver_jobs.JOBS[job_id]

    assert job["lookback_minutes"] == 20
    assert job["states"] == kaspi_order_polling.FAST_ORDER_STATES
    assert job["to_ms"] - job["from_ms"] == 20 * 60 * 1000
    assert job["progress"]["total"] == len(kaspi_order_polling.FAST_ORDER_STATES) + 1


def test_enrichment_repairs_the_full_month_backlog_in_bounded_batches() -> None:
    kaspi_product_enrichment_jobs.JOBS.clear()
    job_id = kaspi_product_enrichment_jobs.create_job(
        days=kaspi_order_polling.ENRICHMENT_LOOKBACK_DAYS,
    )

    job = kaspi_product_enrichment_jobs.JOBS[job_id]
    assert job["days"] == 31
    assert job["lookback_minutes"] is None
    assert kaspi_product_enrichment_jobs.ENRICHMENT_ORDER_LIMIT == 16


def test_fast_polling_never_runs_maintenance_inline(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    stop_event = asyncio.Event()

    async def run_cycle(**options):
        calls.append(options)
        if len(calls) == 10:
            stop_event.set()

    monkeypatch.setattr(kaspi_order_polling, "polling_enabled", lambda: True)
    monkeypatch.setattr(kaspi_order_polling, "polling_startup_delay_seconds", lambda: 0)
    monkeypatch.setattr(kaspi_order_polling, "polling_interval_seconds", lambda: 0.001)
    monkeypatch.setattr(kaspi_order_polling, "run_poll_cycle", run_cycle)

    asyncio.run(kaspi_order_polling.polling_loop(stop_event))

    assert len(calls) == 10
    assert all(call["mode"] == "fast" for call in calls)
    assert all(call["lookback_minutes"] == 20 for call in calls)
    assert all(call["enrich_products"] is False for call in calls)


def test_maintenance_runs_independently_and_uses_deep_cycle_every_hour(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    stop_event = asyncio.Event()

    async def run_cycle(**options):
        calls.append(options)
        if len(calls) == 6:
            stop_event.set()

    monkeypatch.setattr(kaspi_order_polling, "polling_enabled", lambda: True)
    monkeypatch.setattr(
        kaspi_order_polling,
        "maintenance_startup_delay_seconds",
        lambda: 0,
    )
    monkeypatch.setattr(
        kaspi_order_polling,
        "maintenance_interval_seconds",
        lambda: 0.001,
    )
    monkeypatch.setattr(kaspi_order_polling, "run_poll_cycle", run_cycle)

    asyncio.run(kaspi_order_polling.maintenance_polling_loop(stop_event))

    assert [call["mode"] for call in calls] == [
        "full",
        "full",
        "full",
        "full",
        "full",
        "deep",
    ]
    assert all(call["status_store"] is kaspi_order_polling.MAINTENANCE_LAST_RUN for call in calls)


def test_fast_polling_applies_the_same_cycle_to_every_kaspi_workspace(monkeypatch) -> None:
    connections = [
        SimpleNamespace(workspace_id=1, account_id=11),
        SimpleNamespace(workspace_id=2, account_id=22),
    ]
    observed: list[tuple[int, int, str, int | None, bool]] = []

    monkeypatch.setattr(
        kaspi_order_polling,
        "list_workspace_kaspi_connections",
        lambda _session: connections,
    )

    async def run_account(connection, *, days, mode, lookback_minutes, enrich_products):
        observed.append(
            (
                connection.workspace_id,
                days,
                mode,
                lookback_minutes,
                enrich_products,
            )
        )
        return {
            "workspace_id": connection.workspace_id,
            "account_id": connection.account_id,
            "status": "completed",
        }

    monkeypatch.setattr(kaspi_order_polling, "_run_account_cycle", run_account)

    asyncio.run(
        kaspi_order_polling.run_poll_cycle(
            days=1,
            mode="fast",
            lookback_minutes=20,
            enrich_products=True,
        )
    )

    assert observed == [
        (1, 1, "fast", 20, True),
        (2, 1, "fast", 20, True),
    ]
    assert [item["workspace_id"] for item in kaspi_order_polling.LAST_RUN["accounts"]] == [1, 2]


def test_one_failed_account_does_not_skip_the_next_workspace(monkeypatch) -> None:
    connections = [
        SimpleNamespace(workspace_id=1, account_id=11),
        SimpleNamespace(workspace_id=2, account_id=22),
    ]
    observed: list[int] = []

    monkeypatch.setattr(
        kaspi_order_polling,
        "list_workspace_kaspi_connections",
        lambda _session: connections,
    )

    async def run_account(connection, **_options):
        observed.append(connection.workspace_id)
        if connection.workspace_id == 1:
            raise RuntimeError("BARWORK temporary failure")
        return {
            "workspace_id": connection.workspace_id,
            "account_id": connection.account_id,
            "status": "completed",
        }

    monkeypatch.setattr(kaspi_order_polling, "_run_account_cycle", run_account)

    asyncio.run(
        kaspi_order_polling.run_poll_cycle(
            days=1,
            mode="fast",
            lookback_minutes=20,
            enrich_products=False,
        )
    )

    assert observed == [1, 2]
    assert kaspi_order_polling.LAST_RUN["status"] == "failed"
    assert kaspi_order_polling.LAST_RUN["accounts"] == [
        {
            "workspace_id": 1,
            "account_id": 11,
            "mode": "fast",
            "status": "failed",
            "errors": 1,
            "message": "RuntimeError: BARWORK temporary failure",
        },
        {
            "workspace_id": 2,
            "account_id": 22,
            "status": "completed",
        },
    ]


def test_manual_rebuild_marks_orders_ready_before_product_enrichment(monkeypatch) -> None:
    job_id = "manual-rebuild"
    commerce_api.RAW_JOBS.clear()
    commerce_api.RAW_JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "orders_count": 56,
        "errors": [],
    }
    observed: list[bool] = []

    async def run_raw(*_args, **_kwargs) -> None:
        commerce_api.RAW_JOBS[job_id]["status"] = "completed"

    async def run_enrichment(*_args, **_kwargs) -> None:
        observed.append(commerce_api.RAW_JOBS[job_id]["orders_ready"])

    monkeypatch.setattr(commerce_api, "run_job", run_raw)
    monkeypatch.setattr(
        commerce_api,
        "create_product_enrichment_job",
        lambda **_kwargs: "enrichment-job",
    )
    monkeypatch.setattr(commerce_api, "run_product_enrichment_job", run_enrichment)
    monkeypatch.setattr(
        commerce_api,
        "public_product_enrichment_job",
        lambda _job_id: {
            "status": "completed",
            "processed": 0,
            "total": 0,
            "updated": 0,
            "linked": 0,
            "allocated": 0,
            "request_count": 0,
            "errors": [],
        },
    )

    asyncio.run(
        commerce_api._run_full_kaspi_rebuild(
            job_id,
            days=7,
            api_token="token",
            marketplace_account_id=2,
        )
    )

    assert observed == [True]
    assert commerce_api.RAW_JOBS[job_id]["orders_ready"] is True
    assert commerce_api.RAW_JOBS[job_id]["status"] == "completed"


def test_orders_ui_stops_loading_once_orders_are_persisted() -> None:
    source = Path(__file__).resolve().parents[1] / "backend" / "app" / "static" / "orders.js"
    script = source.read_text(encoding="utf-8")
    assert "job.orders_ready === true" in script
