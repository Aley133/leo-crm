"""repair missing browser agent jobs table

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind, table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)

    if "browser_agent_jobs" not in tables:
        op.create_table(
            "browser_agent_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "monitor_target_id",
                sa.Integer(),
                sa.ForeignKey("monitor_targets.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("supplier_product_id", sa.Integer(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("result_payload", sa.Text(), nullable=True),
            sa.Column("error_code", sa.String(length=128), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("lease_token", name="uq_browser_agent_jobs_lease_token"),
        )
    else:
        columns = _column_names(bind, "browser_agent_jobs")
        if "monitor_target_id" not in columns:
            op.add_column("browser_agent_jobs", sa.Column("monitor_target_id", sa.Integer(), nullable=True))
            op.create_foreign_key(
                "fk_browser_agent_jobs_monitor_target",
                "browser_agent_jobs",
                "monitor_targets",
                ["monitor_target_id"],
                ["id"],
                ondelete="CASCADE",
            )

    indexes = _index_names(bind, "browser_agent_jobs")
    required_indexes = {
        "ix_browser_agent_jobs_monitor_target_id": ["monitor_target_id"],
        "ix_browser_agent_jobs_supplier_product_id": ["supplier_product_id"],
        "ix_browser_agent_jobs_status": ["status"],
        "ix_browser_agent_jobs_lease_until": ["lease_until"],
    }
    for name, columns in required_indexes.items():
        if name not in indexes:
            op.create_index(name, "browser_agent_jobs", columns)


def downgrade() -> None:
    # This is a production repair migration. Downgrade is intentionally a no-op
    # so an existing queue table is never destroyed accidentally.
    pass
