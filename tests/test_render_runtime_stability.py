from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
import threading

from fastapi.middleware.gzip import GZipMiddleware
import sqlalchemy as sa

from backend.app import browser_agent_api, kaspi_order_polling, kaspi_product_enrichment_jobs
from backend.app import kaspi_raw_receiver_jobs
from backend.app import telegram_price_alerts
from backend.app.main import app


ROOT = Path(__file__).resolve().parents[1]


def _load_runtime_index_migration():
    path = ROOT / "migrations" / "versions" / "20260811_0031_runtime_query_indexes.py"
    spec = importlib.util.spec_from_file_location("runtime_query_indexes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_automatic_order_loops_are_single_flight_and_use_fixed_delay() -> None:
    source = inspect.getsource(kaspi_order_polling)

    assert source.count("async with order_sync_lock()") >= 3
    assert "await asyncio.to_thread(_workspace_connections)" in source
    assert "await _wait_or_stop(stop_event, polling_interval_seconds())" in source
    assert "max(0.1, polling_interval_seconds() - elapsed)" not in source


def test_price_alert_publisher_keeps_database_work_off_event_loop() -> None:
    source = inspect.getsource(telegram_price_alerts.publish_pending_price_alerts)

    assert "events = await asyncio.to_thread(" in source
    assert source.count("await asyncio.to_thread(") == 3


def test_idle_supplier_agent_claim_is_read_only(db_session, monkeypatch) -> None:
    commits = 0

    def commit() -> None:
        nonlocal commits
        commits += 1

    monkeypatch.setattr(db_session, "commit", commit)
    response = browser_agent_api.claim_browser_agent_job(
        browser_agent_api.BrowserAgentClaim(agent_id="idle-agent"),
        db_session,
    )

    assert response == {
        "job": None,
        "retry_after_seconds": (
            browser_agent_api.BROWSER_AGENT_IDLE_RETRY_SECONDS
        ),
    }
    assert commits == 0


def test_large_api_and_xml_responses_use_gzip_middleware() -> None:
    assert any(
        middleware.cls is GZipMiddleware
        and middleware.kwargs == {"minimum_size": 1024, "compresslevel": 5}
        for middleware in app.user_middleware
    )


def test_runtime_index_migration_skips_tables_created_after_alembic(
    monkeypatch,
) -> None:
    migration = _load_runtime_index_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "marketplace_orders",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("workspace_id", sa.Integer),
        sa.Column("status", sa.String),
        sa.Column("manual_stage", sa.String),
        sa.Column("ordered_at", sa.DateTime),
    )
    metadata.create_all(engine)

    created: list[tuple[str, str, tuple[str, ...]]] = []
    with engine.connect() as connection:
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(
            migration.op,
            "create_index",
            lambda name, table, columns: created.append(
                (name, table, tuple(columns))
            ),
        )
        migration.upgrade()

    assert {name for name, _table, _columns in created} == {
        "ix_marketplace_orders_workspace_status_sort",
        "ix_marketplace_orders_workspace_manual_stage_sort",
    }
    assert all(table == "marketplace_orders" for _name, table, _columns in created)
