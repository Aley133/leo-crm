"""add Kaspi Seller decision timeline

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kaspi_seller_order_timeline_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("kaspi_seller_order_snapshots.id", ondelete="CASCADE"),
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
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_stage", sa.String(length=64), nullable=True),
        sa.Column("to_stage", sa.String(length=64), nullable=True),
        sa.Column("event_payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "snapshot_id",
            "event_type",
            name="uq_kaspi_seller_timeline_snapshot_event",
        ),
    )
    for name, columns in {
        "ix_kaspi_seller_timeline_snapshot_id": ["snapshot_id"],
        "ix_kaspi_seller_timeline_previous_snapshot_id": ["previous_snapshot_id"],
        "ix_kaspi_seller_timeline_merchant_id": ["merchant_id"],
        "ix_kaspi_seller_timeline_order_code": ["order_code"],
        "ix_kaspi_seller_timeline_event_type": ["event_type"],
        "ix_kaspi_seller_timeline_occurred_at": ["occurred_at"],
    }.items():
        op.create_index(name, "kaspi_seller_order_timeline_events", columns)


def downgrade() -> None:
    op.drop_table("kaspi_seller_order_timeline_events")
