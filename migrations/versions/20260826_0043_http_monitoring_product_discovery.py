"""Replace browser monitoring runtime and add product discovery lifecycle.

Revision ID: 20260826_0043
Revises: 20260825_0042
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_0043"
down_revision: str | None = "20260825_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product_test_items", sa.Column("status", sa.String(48), nullable=False, server_default="candidate"))
    op.add_column("product_test_items", sa.Column("product_id", sa.Integer(), nullable=True))
    op.add_column("product_test_items", sa.Column("fast_dumping_policy_id", sa.Integer(), nullable=True))
    op.add_column("product_test_items", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("product_test_items", sa.Column("added_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_product_test_item_product", "product_test_items", "products", ["product_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_product_test_item_fast_policy", "product_test_items", "fast_dumping_policies", ["fast_dumping_policy_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_product_test_items_status", "product_test_items", ["status"])
    op.create_index("ix_product_test_items_product_id", "product_test_items", ["product_id"])

    op.add_column("product_test_jobs", sa.Column("job_type", sa.String(32), nullable=False, server_default="inspect"))
    op.add_column("product_test_jobs", sa.Column("item_id", sa.Integer(), nullable=True))
    op.add_column("product_test_jobs", sa.Column("options_json", sa.JSON(), nullable=False, server_default="{}"))
    op.create_foreign_key("fk_product_test_job_item", "product_test_jobs", "product_test_items", ["item_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_product_test_jobs_job_type", "product_test_jobs", ["job_type"])
    op.create_index("ix_product_test_jobs_item_id", "product_test_jobs", ["item_id"])

    op.create_table(
        "product_test_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("target_new", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("max_kaspi_scan", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("max_ozon_queries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("image_verify", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stock_count", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("preorder_buffer_days", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("minimum_profit_kzt", sa.Numeric(18, 2), nullable=False, server_default="1000"),
        sa.Column("undercut_step_kzt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("allow_price_raise", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_undercut_gap_percent", sa.Numeric(7, 2), nullable=False, server_default="35"),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("delivery_price_premium_kzt", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("delivery_advantage_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("preorder_target_position", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("city_id", sa.String(32), nullable=False, server_default="196220100"),
        sa.Column("zone_id", sa.String(64), nullable=False, server_default="Magnum_ZONE1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", name="uq_product_test_settings_workspace"),
    )
    op.create_index("ix_product_test_settings_workspace_id", "product_test_settings", ["workspace_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE product_test_settings ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL PRIVILEGES ON TABLE product_test_settings FROM PUBLIC")
        op.execute("REVOKE ALL PRIVILEGES ON SEQUENCE product_test_settings_id_seq FROM PUBLIC")
        op.execute(
            """
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON TABLE product_test_settings FROM anon;
                REVOKE ALL PRIVILEGES ON SEQUENCE product_test_settings_id_seq FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL PRIVILEGES ON TABLE product_test_settings FROM authenticated;
                REVOKE ALL PRIVILEGES ON SEQUENCE product_test_settings_id_seq FROM authenticated;
              END IF;
            END $$;
            """
        )


def downgrade() -> None:
    op.drop_table("product_test_settings")
    op.drop_index("ix_product_test_jobs_item_id", table_name="product_test_jobs")
    op.drop_index("ix_product_test_jobs_job_type", table_name="product_test_jobs")
    op.drop_constraint("fk_product_test_job_item", "product_test_jobs", type_="foreignkey")
    op.drop_column("product_test_jobs", "options_json")
    op.drop_column("product_test_jobs", "item_id")
    op.drop_column("product_test_jobs", "job_type")
    op.drop_index("ix_product_test_items_product_id", table_name="product_test_items")
    op.drop_index("ix_product_test_items_status", table_name="product_test_items")
    op.drop_constraint("fk_product_test_item_fast_policy", "product_test_items", type_="foreignkey")
    op.drop_constraint("fk_product_test_item_product", "product_test_items", type_="foreignkey")
    op.drop_column("product_test_items", "added_at")
    op.drop_column("product_test_items", "last_error")
    op.drop_column("product_test_items", "fast_dumping_policy_id")
    op.drop_column("product_test_items", "product_id")
    op.drop_column("product_test_items", "status")
