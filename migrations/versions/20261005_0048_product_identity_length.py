"""Support full Kaspi XML merchant SKU identities.

Revision ID: 20261005_0048
Revises: 20260928_0047
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20261005_0048"
down_revision: str | None = "20260928_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.alter_column(
            "kaspi_product_id",
            existing_type=sa.String(64),
            type_=sa.String(128),
            existing_nullable=False,
        )


def downgrade() -> None:
    # PostgreSQL refuses shrinking while long identities exist, preserving
    # data instead of truncating IDs and merging unrelated products.
    with op.batch_alter_table("products") as batch:
        batch.alter_column(
            "kaspi_product_id",
            existing_type=sa.String(128),
            type_=sa.String(64),
            existing_nullable=False,
        )
