from __future__ import annotations

import asyncio
import inspect
import threading

from backend.app import kaspi_order_polling, kaspi_product_enrichment_jobs
from backend.app import kaspi_raw_receiver_jobs
from backend.app import telegram_price_alerts


def test_raw_order_persistence_releases_connection_between_bounded_batches(
    monkeypatch,
) -> None:
    calls: list[tuple[list[int], str, int]] = []
    main_thread_id = threading.get_ident()

    def persist(batch, *, timezone_name):
        calls.append((list(batch), timezone_name, threading.get_ident()))
        return len(batch), 1

    monkeypatch.setattr(kaspi_raw_receiver_jobs, "ORDER_PERSIST_BATCH_SIZE", 2)
    monkeypatch.setattr(kaspi_raw_receiver_jobs, "_persist_orders", persist)

    imported, updated = asyncio.run(
        kaspi_raw_receiver_jobs._persist_orders_in_batches(
            [1, 2, 3, 4, 5],
            timezone_name="Asia/Almaty",
        )
    )

    assert imported == 5
    assert updated == 3
    assert [batch for batch, _timezone, _thread in calls] == [[1, 2], [3, 4], [5]]
    assert {timezone for _batch, timezone, _thread in calls} == {"Asia/Almaty"}
    assert all(thread_id != main_thread_id for _batch, _timezone, thread_id in calls)


def test_automatic_polling_waits_for_api_startup_grace_period(monkeypatch) -> None:
    calls: list[dict] = []

    async def run_cycle(**options):
        calls.append(options)
        stop_event.set()

    stop_event = asyncio.Event()
    monkeypatch.setattr(kaspi_order_polling, "polling_enabled", lambda: True)
    monkeypatch.setattr(
        kaspi_order_polling,
        "polling_startup_delay_seconds",
        lambda: 0.02,
    )
    monkeypatch.setattr(kaspi_order_polling, "run_poll_cycle", run_cycle)

    async def scenario() -> None:
        task = asyncio.create_task(kaspi_order_polling.polling_loop(stop_event))
        await asyncio.sleep(0)
        assert calls == []
        await task

    asyncio.run(scenario())

    assert calls == [
        {
            "days": 1,
            "mode": "fast",
            "lookback_minutes": kaspi_order_polling.FAST_LOOKBACK_MINUTES,
            "enrich_products": False,
        }
    ]


def test_finished_in_memory_job_history_is_bounded(monkeypatch) -> None:
    raw_jobs = {
        str(index): {"status": "completed"}
        for index in range(kaspi_raw_receiver_jobs.JOB_HISTORY_LIMIT + 10)
    }
    enrichment_jobs = {
        str(index): {"status": "completed"}
        for index in range(kaspi_product_enrichment_jobs.JOB_HISTORY_LIMIT + 10)
    }
    monkeypatch.setattr(kaspi_raw_receiver_jobs, "JOBS", raw_jobs)
    monkeypatch.setattr(kaspi_product_enrichment_jobs, "JOBS", enrichment_jobs)

    kaspi_raw_receiver_jobs.create_job(days=1)
    kaspi_product_enrichment_jobs.create_job(days=1)

    assert len(raw_jobs) == kaspi_raw_receiver_jobs.JOB_HISTORY_LIMIT
    assert len(enrichment_jobs) == kaspi_product_enrichment_jobs.JOB_HISTORY_LIMIT


def test_product_enrichment_keeps_blocking_database_work_off_event_loop() -> None:
    source = inspect.getsource(kaspi_product_enrichment_jobs.run_job)

    assert "await asyncio.to_thread(_load_unresolved_orders, since)" in source
    assert ") = await asyncio.to_thread(\n                        _persist_enriched_order," in source
    assert "database_write_semaphore = asyncio.Semaphore(1)" in source


def test_price_alert_publisher_keeps_database_work_off_event_loop() -> None:
    source = inspect.getsource(telegram_price_alerts.publish_pending_price_alerts)

    assert "events = await asyncio.to_thread(" in source
    assert source.count("await asyncio.to_thread(") == 3
