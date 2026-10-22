import json
import logging

import pytest
from sqlalchemy import create_engine, text

from backend.app.fast_dumping_diagnostics import _TRACE, trace_fast_operation


def _profiles(caplog):
    return [json.loads(r.getMessage().split("fast_profile ", 1)[1])
            for r in caplog.records if r.getMessage().startswith("fast_profile ")]


def test_counts_only_scoped_queries_and_never_logs_sql_or_values(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("select 1"))
        with trace_fast_operation("scan_complete", 3, job_id=42) as trace:
            conn.execute(text("select :secret"), {"secret": "PRIVATE_LEASE_AND_TOKEN"})
            conn.execute(text("select 2"))
            trace.outcome = "queued_apply"
        conn.execute(text("select 3"))
    engine.dispose()
    record, = _profiles(caplog)
    assert record["queries"] == 2
    assert record["workspace"] == 3 and record["job"] == 42
    assert record["elapsed_ms"] >= record["sql_ms"] >= 0
    assert record["outcome"] == "queued_apply"
    assert "PRIVATE_LEASE_AND_TOKEN" not in caplog.text
    assert "select" not in caplog.text
    assert _TRACE.get() is None


def test_failed_nested_operation_restores_the_outer_trace(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        with trace_fast_operation("claim", 1):
            conn.execute(text("select 1"))
            with pytest.raises(ValueError):
                with trace_fast_operation("claim", 3):
                    conn.execute(text("select 2"))
                    raise ValueError("PRIVATE_ERROR_TEXT")
            conn.execute(text("select 3"))
    engine.dispose()
    inner, outer = _profiles(caplog)
    assert (inner["workspace"], inner["queries"], inner["outcome"]) == (3, 1, "error")
    assert (outer["workspace"], outer["queries"]) == (1, 2)
    assert "PRIVATE_ERROR_TEXT" not in caplog.text
    assert _TRACE.get() is None
