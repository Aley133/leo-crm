"""add dumping engine and XML feed tables

Revision ID: 20260724_0015
Revises: 20260724_0014
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260724_0015"
down_revision: str | None = "20260724_0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "dumping_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("minimum_profit_kzt", sa.Numeric(18, 2), server_default="1000", nullable=False),
        sa.Column("undercut_step_kzt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supplier_delivery_buffer_days", sa.Integer(), server_default="1", nullable=False),
        sa.Column("inventory_first", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auto_publish_xml", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("city_id", sa.String(length=32), server_default="750000000", nullable=False),
        sa.Column("zone_id", sa.String(length=64), server_default="Magnum_ZONE1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", name="uq_dumping_policy_product"),
    )
    op.create_index("ix_dumping_policies_product_id", "dumping_policies", ["product_id"])

    op.create_table(
        "dumping_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("dumping_policy_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_cost_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("source_delivery_days", sa.Integer(), nullable=True),
        sa.Column("safe_floor_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("own_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("competitor_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("target_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("preorder_days", sa.Integer(), nullable=True),
        sa.Column("published", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("explanation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dumping_policy_id"], ["dumping_policies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dumping_runs_product_id", "dumping_runs", ["product_id"])
    op.create_index("ix_dumping_runs_dumping_policy_id", "dumping_runs", ["dumping_policy_id"])
    op.create_index("ix_dumping_runs_status", "dumping_runs", ["status"])
    op.create_index("ix_dumping_runs_created_at", "dumping_runs", ["created_at"])

    op.create_table(
        "kaspi_xml_feeds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.String(length=128), nullable=True),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_xml", sa.Text(), nullable=False),
        sa.Column("generated_xml", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("kaspi_xml_feeds")
    op.drop_table("dumping_runs")
    op.drop_table("dumping_policies")
