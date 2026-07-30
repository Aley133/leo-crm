from sqlalchemy.dialects import postgresql

from backend.app import browser_agent_api
from backend.app.browser_agent_dispatch import build_due_browser_targets_statement


def test_due_browser_dispatch_compiles_for_postgresql_skip_locked() -> None:
    statement = build_due_browser_targets_statement(limit=100, supplier_code="OZON")
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "FOR UPDATE OF MONITOR_TARGETS SKIP LOCKED" in sql
    assert "NOT IN" in sql
    assert "BROWSER_AGENT_JOBS.MONITOR_TARGET_ID IS NOT NULL" in sql
    assert "SUPPLIERS.CODE = 'OZON'" in sql


def test_duplicate_dispatchers_share_one_short_database_scan_window() -> None:
    browser_agent_api._DISPATCH_LAST_AT.clear()
    try:
        acquired, retry_after = browser_agent_api._acquire_dispatch_slot(
            "OZON",
            now=100.0,
        )
        duplicate, duplicate_retry_after = browser_agent_api._acquire_dispatch_slot(
            "ozon",
            now=101.0,
        )
        other_supplier, _ = browser_agent_api._acquire_dispatch_slot(
            "wb",
            now=101.0,
        )
        next_window, _ = browser_agent_api._acquire_dispatch_slot(
            "ozon",
            now=100.0 + browser_agent_api.DISPATCH_MIN_INTERVAL_SECONDS,
        )
    finally:
        browser_agent_api._DISPATCH_LAST_AT.clear()

    assert acquired is True
    assert retry_after == 0
    assert duplicate is False
    assert duplicate_retry_after > 0
    assert other_supplier is True
    assert next_window is True
