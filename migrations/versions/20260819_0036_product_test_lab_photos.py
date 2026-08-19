"""Add Product Test Lab and lightweight product photos.

Revision ID: 20260819_0036
Revises: 20260818_0035
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260819_0036"
down_revision: str | None = "20260818_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("products", sa.Column("image_url", sa.String(length=2048), nullable=True))
    op.create_table(
        "product_test_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("input_reference", sa.Text(), nullable=False),
        sa.Column("kaspi_product_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_sku", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.String(length=2048), nullable=True),
        sa.Column("kaspi_url", sa.Text(), nullable=False),
        sa.Column("supplier_url", sa.Text(), nullable=True),
        sa.Column("observed_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("test_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("preorder_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stock_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("city_id", sa.String(length=32), nullable=False, server_default="196220100"),
        sa.Column("zone_id", sa.String(length=64), nullable=False, server_default="Magnum_ZONE1"),
        sa.Column("offers_json", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "merchant_sku", name="uq_product_test_workspace_sku"),
    )
    op.create_index("ix_product_test_items_workspace_id", "product_test_items", ["workspace_id"])
    op.create_index("ix_product_test_items_kaspi_product_id", "product_test_items", ["kaspi_product_id"])
    op.create_index("ix_product_test_items_workspace_active_updated", "product_test_items", ["workspace_id", "active", "updated_at"])
    op.create_table(
        "product_test_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("input_reference", sa.Text(), nullable=False),
        sa.Column("city_id", sa.String(length=32), nullable=False, server_default="196220100"),
        sa.Column("zone_id", sa.String(length=64), nullable=False, server_default="Magnum_ZONE1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_product_test_jobs_workspace_id", "product_test_jobs", ["workspace_id"])
    op.create_index("ix_product_test_jobs_status", "product_test_jobs", ["status"])
    op.create_index("ix_product_test_jobs_workspace_status_created", "product_test_jobs", ["workspace_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_table("product_test_jobs")
    op.drop_table("product_test_items")
    op.drop_column("products", "image_url")
