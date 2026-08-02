from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app import kaspi_order_polling, kaspi_raw_receiver_jobs


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


def test_polling_runs_fast_each_minute_and_full_reconciliation_every_tenth_cycle(
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

    assert all(call["mode"] == "fast" for call in calls[:9])
    assert all(call["lookback_minutes"] == 20 for call in calls[:9])
    assert calls[9] == {"days": 1, "mode": "full", "enrich_products": True}


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
            enrich_products=False,
        )
    )

    assert observed == [
        (1, 1, "fast", 20, False),
        (2, 1, "fast", 20, False),
    ]
    assert [item["workspace_id"] for item in kaspi_order_polling.LAST_RUN["accounts"]] == [1, 2]
