"""Retain marketplace source order status and revision.

Revision ID: 20260719_0008
Revises: 20260719_0007
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0008"
down_revision: str | None = "20260719_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marketplace_orders",
        sa.Column("original_status", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "marketplace_orders",
        sa.Column("source_revision", sa.String(length=128), nullable=True),
    )
    op.execute(
        "UPDATE marketplace_orders SET original_status = status "
        "WHERE original_status IS NULL"
    )
    op.alter_column("marketplace_orders", "original_status", nullable=False)
    op.create_index(
        "ix_marketplace_orders_original_status",
        "marketplace_orders",
        ["original_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_orders_original_status", table_name="marketplace_orders")
    op.drop_column("marketplace_orders", "source_revision")
    op.drop_column("marketplace_orders", "original_status")
