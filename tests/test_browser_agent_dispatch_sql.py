from sqlalchemy.dialects import postgresql

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
