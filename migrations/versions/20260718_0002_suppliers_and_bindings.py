"""Suppliers, supplier products and product bindings.

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260718_0002"
down_revision: str | None = "20260718_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_suppliers_code"),
    )
    op.create_index("ix_suppliers_code", "suppliers", ["code"])
    op.create_index("ix_suppliers_is_active", "suppliers", ["is_active"])

    op.create_table(
        "supplier_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("current_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("supplier_id", "external_id", name="uq_supplier_product_external"),
    )
    op.create_index("ix_supplier_products_supplier_id", "supplier_products", ["supplier_id"])

    op.create_table(
        "product_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_product_id", sa.Integer(), sa.ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence_score", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_id", "supplier_product_id", name="uq_product_binding"),
    )
    op.create_index("ix_product_bindings_product_id", "product_bindings", ["product_id"])
    op.create_index("ix_product_bindings_supplier_product_id", "product_bindings", ["supplier_product_id"])


def downgrade() -> None:
    op.drop_table("product_bindings")
    op.drop_table("supplier_products")
    op.drop_table("suppliers")
