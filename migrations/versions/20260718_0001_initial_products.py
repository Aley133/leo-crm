"""Initial products table.

Revision ID: 20260718_0001
Revises:
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260718_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "products" in inspector.get_table_names():
        return

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kaspi_product_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_sku", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("kaspi_product_id", name="uq_products_kaspi_product_id"),
    )
    op.create_index("ix_products_kaspi_product_id", "products", ["kaspi_product_id"])
    op.create_index("ix_products_merchant_sku", "products", ["merchant_sku"])
    op.create_index("ix_products_brand", "products", ["brand"])
    op.create_index("ix_products_status", "products", ["status"])


def downgrade() -> None:
    op.drop_index("ix_products_status", table_name="products")
    op.drop_index("ix_products_brand", table_name="products")
    op.drop_index("ix_products_merchant_sku", table_name="products")
    op.drop_index("ix_products_kaspi_product_id", table_name="products")
    op.drop_table("products")
