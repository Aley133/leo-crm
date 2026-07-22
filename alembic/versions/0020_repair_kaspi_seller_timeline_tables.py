"""repair Kaspi Seller snapshot and timeline tables

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "kaspi_seller_order_snapshots" not in tables:
        op.create_table(
            "kaspi_seller_order_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("browser_agent_job_id", sa.Integer(), sa.ForeignKey("browser_agent_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("previous_snapshot_id", sa.Integer(), sa.ForeignKey("kaspi_seller_order_snapshots.id", ondelete="SET NULL"), nullable=True),
            sa.Column("merchant_id", sa.String(length=128), nullable=False),
            sa.Column("order_code", sa.String(length=128), nullable=False),
            sa.Column("state", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=128), nullable=False),
            sa.Column("stage", sa.String(length=64), nullable=True),
            sa.Column("schema_version", sa.String(length=64), nullable=True),
            sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("changed", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("snapshot_payload", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("browser_agent_job_id", name="uq_kaspi_seller_snapshots_browser_job"),
        )

    snapshot_indexes = _indexes(bind, "kaspi_seller_order_snapshots")
    for name, columns in {
        "ix_kaspi_seller_snapshots_browser_agent_job_id": ["browser_agent_job_id"],
        "ix_kaspi_seller_snapshots_previous_snapshot_id": ["previous_snapshot_id"],
        "ix_kaspi_seller_snapshots_merchant_id": ["merchant_id"],
        "ix_kaspi_seller_snapshots_order_code": ["order_code"],
        "ix_kaspi_seller_snapshots_state": ["state"],
        "ix_kaspi_seller_snapshots_status": ["status"],
        "ix_kaspi_seller_snapshots_stage": ["stage"],
        "ix_kaspi_seller_snapshots_fingerprint": ["snapshot_fingerprint"],
        "ix_kaspi_seller_snapshots_observed_at": ["observed_at"],
        "ix_kaspi_seller_snapshots_order_history": ["merchant_id", "order_code", "observed_at"],
    }.items():
        if name not in snapshot_indexes:
            op.create_index(name, "kaspi_seller_order_snapshots", columns)

    tables = _tables(bind)
    if "kaspi_seller_order_timeline_events" not in tables:
        op.create_table(
            "kaspi_seller_order_timeline_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("kaspi_seller_order_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("previous_snapshot_id", sa.Integer(), sa.ForeignKey("kaspi_seller_order_snapshots.id", ondelete="SET NULL"), nullable=True),
            sa.Column("merchant_id", sa.String(length=128), nullable=False),
            sa.Column("order_code", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("from_stage", sa.String(length=64), nullable=True),
            sa.Column("to_stage", sa.String(length=64), nullable=True),
            sa.Column("event_payload", sa.Text(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("snapshot_id", "event_type", name="uq_kaspi_seller_timeline_snapshot_event"),
        )

    timeline_indexes = _indexes(bind, "kaspi_seller_order_timeline_events")
    for name, columns in {
        "ix_kaspi_seller_timeline_snapshot_id": ["snapshot_id"],
        "ix_kaspi_seller_timeline_previous_snapshot_id": ["previous_snapshot_id"],
        "ix_kaspi_seller_timeline_merchant_id": ["merchant_id"],
        "ix_kaspi_seller_timeline_order_code": ["order_code"],
        "ix_kaspi_seller_timeline_event_type": ["event_type"],
        "ix_kaspi_seller_timeline_occurred_at": ["occurred_at"],
    }.items():
        if name not in timeline_indexes:
            op.create_index(name, "kaspi_seller_order_timeline_events", columns)


def downgrade() -> None:
    pass
