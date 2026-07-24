"""add daily revenue snapshots

Revision ID: 20260724_0014
Revises: 20260723_0013
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260724_0014"
down_revision: str | None = "20260723_0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "daily_revenue_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("source_stage", sa.String(length=32), nullable=False),
        sa.Column("orders_count", sa.Integer(), nullable=False),
        sa.Column("units_count", sa.Integer(), nullable=False),
        sa.Column("revenue", sa.Numeric(18, 2), nullable=False),
        sa.Column("net_profit", sa.Numeric(18, 2), nullable=False),
        sa.Column("margin_pct", sa.Numeric(9, 4), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_account_id",
            "business_date",
            name="uq_daily_revenue_snapshot_account_date",
        ),
    )
    op.create_index(
        "ix_daily_revenue_snapshots_marketplace_account_id",
        "daily_revenue_snapshots",
        ["marketplace_account_id"],
    )
    op.create_index(
        "ix_daily_revenue_snapshots_business_date",
        "daily_revenue_snapshots",
        ["business_date"],
    )


def downgrade() -> None:
    op.drop_table("daily_revenue_snapshots")
