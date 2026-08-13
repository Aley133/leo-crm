"""Bound Fast Dumping request frequency and completed job history.

Revision ID: 20260813_0033
Revises: 20260812_0032
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_0033"
down_revision: str | None = "20260812_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fast_dumping_jobs",
        sa.Column("not_before_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fast_dumping_jobs_not_before_at",
        "fast_dumping_jobs",
        ["not_before_at"],
    )
    op.create_index(
        "ix_fast_dumping_jobs_workspace_status_due_id",
        "fast_dumping_jobs",
        ["workspace_id", "status", "not_before_at", "id"],
    )

    # Existing realtime policies used a dangerous 10-second default. Move all
    # sub-five-minute policies to the safe 10-minute default without shortening
    # any deliberately slower policy.
    op.execute(
        sa.text(
            "UPDATE fast_dumping_policies "
            "SET scan_interval_seconds = 600 "
            "WHERE scan_interval_seconds < 300"
        )
    )
    op.alter_column(
        "fast_dumping_policies",
        "scan_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("600"),
    )

    # The UI does not expose historical jobs. Keep the newest 200 completed
    # diagnostics per product and preserve every active/incomplete operation.
    op.execute(
        sa.text(
            "DELETE FROM fast_dumping_jobs WHERE id IN ("
            "SELECT id FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY workspace_id, product_id ORDER BY id DESC"
            ") AS history_rank "
            "FROM fast_dumping_jobs WHERE completed_at IS NOT NULL"
            ") AS ranked WHERE history_rank > 200"
            ")"
        )
    )


def downgrade() -> None:
    op.alter_column(
        "fast_dumping_policies",
        "scan_interval_seconds",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("10"),
    )
    op.drop_index(
        "ix_fast_dumping_jobs_workspace_status_due_id",
        table_name="fast_dumping_jobs",
    )
    op.drop_index(
        "ix_fast_dumping_jobs_not_before_at",
        table_name="fast_dumping_jobs",
    )
    op.drop_column("fast_dumping_jobs", "not_before_at")
