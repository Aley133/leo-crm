"""add Kaspi Seller snapshot history

Revision ID: 0018
Revises: 0017
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kaspi_seller_order_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "browser_agent_job_id",
            sa.Integer(),
            sa.ForeignKey("browser_agent_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "previous_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("kaspi_seller_order_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "browser_agent_job_id",
            name="uq_kaspi_seller_snapshots_browser_job",
        ),
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_browser_agent_job_id",
        "kaspi_seller_order_snapshots",
        ["browser_agent_job_id"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_previous_snapshot_id",
        "kaspi_seller_order_snapshots",
        ["previous_snapshot_id"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_merchant_id",
        "kaspi_seller_order_snapshots",
        ["merchant_id"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_order_code",
        "kaspi_seller_order_snapshots",
        ["order_code"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_state",
        "kaspi_seller_order_snapshots",
        ["state"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_status",
        "kaspi_seller_order_snapshots",
        ["status"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_stage",
        "kaspi_seller_order_snapshots",
        ["stage"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_fingerprint",
        "kaspi_seller_order_snapshots",
        ["snapshot_fingerprint"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_observed_at",
        "kaspi_seller_order_snapshots",
        ["observed_at"],
    )
    op.create_index(
        "ix_kaspi_seller_snapshots_order_history",
        "kaspi_seller_order_snapshots",
        ["merchant_id", "order_code", "observed_at"],
    )


def downgrade() -> None:
    op.drop_table("kaspi_seller_order_snapshots")
