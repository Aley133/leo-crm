"""Add leased local-agent product photo backfill.

Revision ID: 20260819_0037
Revises: 20260819_0036
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260819_0037"
down_revision: str | None = "20260819_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("image_backfill_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("image_backfill_lease_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("image_backfill_agent_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("image_backfill_error", sa.String(length=1000), nullable=True),
    )
    op.create_index(
        "ix_products_image_backfill_after",
        "products",
        ["image_backfill_after"],
    )


def downgrade() -> None:
    op.drop_index("ix_products_image_backfill_after", table_name="products")
    op.drop_column("products", "image_backfill_error")
    op.drop_column("products", "image_backfill_agent_id")
    op.drop_column("products", "image_backfill_lease_token")
    op.drop_column("products", "image_backfill_after")
