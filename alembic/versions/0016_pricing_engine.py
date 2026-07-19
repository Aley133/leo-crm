"""add pricing engine

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("target_margin_pct", sa.Numeric(7, 4), nullable=False, server_default="30"),
        sa.Column("marketplace_fee_pct", sa.Numeric(7, 4), nullable=False, server_default="12"),
        sa.Column("payment_fee_pct", sa.Numeric(7, 4), nullable=False, server_default="3"),
        sa.Column("delivery_cost_kzt", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("fixed_cost_kzt", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("minimum_price_kzt", sa.Numeric(14, 2), nullable=True),
        sa.Column("rounding_step_kzt", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_id", name="uq_pricing_policy_product"),
    )
    op.create_index("ix_pricing_policies_product_id", "pricing_policies", ["product_id"])

    op.create_table(
        "fx_rate_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fx_rate_snapshots_base_currency", "fx_rate_snapshots", ["base_currency"])
    op.create_index("ix_fx_rate_snapshots_quote_currency", "fx_rate_snapshots", ["quote_currency"])
    op.create_index("ix_fx_rate_snapshots_observed_at", "fx_rate_snapshots", ["observed_at"])

    op.create_table(
        "price_calculations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pricing_policy_id", sa.Integer(), sa.ForeignKey("pricing_policies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("supplier_offer_state_id", sa.Integer(), sa.ForeignKey("supplier_offer_states.id", ondelete="SET NULL"), nullable=True),
        sa.Column("fx_rate_snapshot_id", sa.Integer(), sa.ForeignKey("fx_rate_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("supplier_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("supplier_currency", sa.String(length=3), nullable=True),
        sa.Column("fx_rate_to_kzt", sa.Numeric(18, 8), nullable=True),
        sa.Column("supplier_cost_kzt", sa.Numeric(14, 2), nullable=True),
        sa.Column("delivery_cost_kzt", sa.Numeric(14, 2), nullable=True),
        sa.Column("fixed_cost_kzt", sa.Numeric(14, 2), nullable=True),
        sa.Column("total_fee_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("target_margin_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("recommended_price_kzt", sa.Numeric(14, 2), nullable=True),
        sa.Column("explanation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_price_calculations_product_id", "price_calculations", ["product_id"])
    op.create_index("ix_price_calculations_pricing_policy_id", "price_calculations", ["pricing_policy_id"])
    op.create_index("ix_price_calculations_supplier_offer_state_id", "price_calculations", ["supplier_offer_state_id"])
    op.create_index("ix_price_calculations_fx_rate_snapshot_id", "price_calculations", ["fx_rate_snapshot_id"])
    op.create_index("ix_price_calculations_status", "price_calculations", ["status"])
    op.create_index("ix_price_calculations_created_at", "price_calculations", ["created_at"])


def downgrade() -> None:
    op.drop_table("price_calculations")
    op.drop_table("fx_rate_snapshots")
    op.drop_table("pricing_policies")
