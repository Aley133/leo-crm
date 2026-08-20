"""Bound large JSON and diagnostic histories.

Revision ID: 20260820_0039
Revises: 20260820_0038
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260820_0039"
down_revision: str | None = "20260820_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _delete_ranked_history(
    *,
    table: str,
    partition_by: str,
    order_by: str,
    keep: int,
    where: str = "1=1",
) -> None:
    # Every identifier and predicate is a migration-owned constant, never user
    # input. The nested SELECT keeps this portable across PostgreSQL and SQLite
    # migration tests while deleting only rows beyond the explicit boundary.
    op.execute(
        sa.text(
            f"DELETE FROM {table} WHERE id IN ("
            "SELECT id FROM ("
            f"SELECT id, ROW_NUMBER() OVER (PARTITION BY {partition_by} "
            f"ORDER BY {order_by}) AS history_rank FROM {table} WHERE {where}"
            f") AS ranked WHERE history_rank > {int(keep)}"
            ")"
        )
    )


def upgrade() -> None:
    _delete_ranked_history(
        table="marketplace_raw_payloads",
        partition_by="marketplace_account_id, payload_type, external_object_id",
        order_by="received_at DESC, id DESC",
        keep=20,
        where="payload_type = 'order'",
    )
    _delete_ranked_history(
        table="dumping_runs",
        partition_by="workspace_id, product_id",
        order_by="id DESC",
        keep=100,
        where="status NOT IN ('queued_local', 'leased_local')",
    )
    _delete_ranked_history(
        table="fast_dumping_jobs",
        partition_by="workspace_id, product_id",
        order_by="id DESC",
        keep=100,
        where="completed_at IS NOT NULL",
    )
    _delete_ranked_history(
        table="product_test_jobs",
        partition_by="workspace_id",
        order_by="id DESC",
        keep=40,
        where="completed_at IS NOT NULL",
    )


def downgrade() -> None:
    # Deleted redundant history cannot and should not be synthesized again.
    pass
