from __future__ import annotations

"""Idempotently repair the browser-agent queue schema in an existing database.

This command is a deployment safeguard for installations whose Alembic history was
previously stamped without the browser_agent_jobs table. It never drops data and
creates only the missing queue table/indexes represented by the current ORM model.
"""

from sqlalchemy import inspect

from backend.app import monitoring  # noqa: F401  # register monitor_targets metadata
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.db import engine


_REQUIRED_COLUMNS = {
    "id",
    "monitor_target_id",
    "supplier_product_id",
    "url",
    "status",
    "lease_owner",
    "lease_token",
    "lease_until",
    "result_payload",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
    "finished_at",
}


def ensure_browser_agent_schema() -> None:
    BrowserAgentJob.__table__.create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    if "browser_agent_jobs" not in inspector.get_table_names():
        raise RuntimeError("browser_agent_jobs was not created")

    columns = {column["name"] for column in inspector.get_columns("browser_agent_jobs")}
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            "browser_agent_jobs exists but is missing required columns: " + ", ".join(missing)
        )

    print("browser_agent_jobs schema is ready")


if __name__ == "__main__":
    ensure_browser_agent_schema()
