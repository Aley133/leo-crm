"""Bounded, payload-free timings for the Fast Agent's hot API path."""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.engine import Engine


_LOGGER = logging.getLogger("uvicorn.error")
_TABLE = re.compile(r"\b(?:FROM|UPDATE|INTO)\s+([a-z_][a-z_0-9]*)", re.I)


@dataclass
class FastTrace:
    stage: str
    workspace_id: int
    job_id: int | None = None
    lane: str = "all"
    outcome: str | None = None
    queries: int = 0
    sql_seconds: float = 0
    tables: dict = field(default_factory=dict)


_TRACE: ContextVar[FastTrace | None] = ContextVar("fast_api_trace", default=None)


@event.listens_for(Engine, "before_cursor_execute")
def _before_sql(_conn, _cursor, statement, _parameters, context, _many):
    trace = _TRACE.get()
    if trace is None:
        return
    match = _TABLE.search(statement)
    table = match.group(1).lower() if match else "other"
    if table not in trace.tables and len(trace.tables) >= 12:
        table = "other"
    trace.queries += 1
    context._fast_trace_timing = (trace, table, perf_counter())


@event.listens_for(Engine, "after_cursor_execute")
def _after_sql(_conn, _cursor, _statement, _parameters, context, _many):
    timing = getattr(context, "_fast_trace_timing", None)
    if timing is None:
        return
    trace, table, started = timing
    elapsed = perf_counter() - started
    trace.sql_seconds += elapsed
    count, seconds = trace.tables.get(table, (0, 0))
    trace.tables[table] = (count + 1, seconds + elapsed)


@contextmanager
def trace_fast_operation(stage: str, workspace_id: int, *, job_id=None, lane="all"):
    trace = FastTrace(stage, workspace_id, job_id, lane)
    token = _TRACE.set(trace)
    started = perf_counter()
    failed = False
    try:
        yield trace
    except BaseException:
        failed = True
        raise
    finally:
        _TRACE.reset(token)
        # Do not log request bodies, agent identities, SQL, parameters, tokens,
        # lease values, product names or exception text.
        _LOGGER.info("fast_profile %s", json.dumps({
            "stage": trace.stage, "workspace": trace.workspace_id,
            "job": trace.job_id, "lane": trace.lane,
            "outcome": "error" if failed else trace.outcome,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
            "sql_ms": round(trace.sql_seconds * 1000, 1), "queries": trace.queries,
            "tables": {k: {"count": v[0], "ms": round(v[1] * 1000, 1)}
                       for k, v in trace.tables.items()},
        }, separators=(",", ":")))
