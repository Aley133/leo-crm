"""Add composite indexes for bounded order and agent runtime queries.

Revision ID: 20260811_0031
Revises: 20260805_0030
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260811_0031"
down_revision: str | None = "20260805_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_marketplace_orders_workspace_status_sort",
        "marketplace_orders",
        ("workspace_id", "status", "ordered_at", "id"),
    ),
    (
        "ix_marketplace_orders_workspace_manual_stage_sort",
        "marketplace_orders",
        ("workspace_id", "manual_stage", "ordered_at", "id"),
    ),
    (
        "ix_marketplace_order_lines_workspace_order",
        "marketplace_order_lines",
        ("workspace_id", "marketplace_order_id", "id"),
    ),
    (
        "ix_inventory_allocations_workspace_line",
        "inventory_allocations",
        ("workspace_id", "marketplace_order_line_id", "inventory_batch_id"),
    ),
    (
        "ix_marketplace_raw_payloads_latest_order",
        "marketplace_raw_payloads",
        (
            "workspace_id",
            "marketplace_account_id",
            "payload_type",
            "external_object_id",
            "received_at",
            "id",
        ),
    ),
    (
        "ix_dumping_runs_workspace_status_id",
        "dumping_runs",
        ("workspace_id", "status", "id"),
    ),
    (
        "ix_browser_agent_jobs_status_id",
        "browser_agent_jobs",
        ("status", "id"),
    ),
    (
        "ix_monitor_targets_status_due",
        "monitor_targets",
        ("status", "next_check_at", "id"),
    ),
)


def _quote(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _indexes_for_existing_tables() -> tuple[
    tuple[str, str, tuple[str, ...]], ...
]:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    return tuple(
        index_definition
        for index_definition in INDEXES
        if index_definition[1] in existing_tables
    )


def upgrade() -> None:
    indexes = _indexes_for_existing_tables()
    if op.get_bind().dialect.name == "postgresql":
        # These tables are continuously read and written in production.  Build
        # each index outside the migration transaction so deploy does not hold
        # a table-wide write lock while historical rows are scanned.
        with op.get_context().autocommit_block():
            for name, table, columns in indexes:
                column_sql = ", ".join(_quote(column) for column in columns)
                op.execute(
                    sa.text(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_quote(name)} "
                        f"ON {_quote(table)} ({column_sql})"
                    )
                )
        return

    inspector = sa.inspect(op.get_bind())
    for name, table, columns in indexes:
        existing = {
            index.get("name")
            for index in inspector.get_indexes(table)
            if index.get("name")
        }
        if name not in existing:
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    indexes = _indexes_for_existing_tables()
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _columns in reversed(indexes):
                op.execute(
                    sa.text(
                        f"DROP INDEX CONCURRENTLY IF EXISTS {_quote(name)}"
                    )
                )
        return

    inspector = sa.inspect(op.get_bind())
    for name, table, _columns in reversed(indexes):
        existing = {
            index.get("name")
            for index in inspector.get_indexes(table)
            if index.get("name")
        }
        if name in existing:
            op.drop_index(name, table_name=table)
