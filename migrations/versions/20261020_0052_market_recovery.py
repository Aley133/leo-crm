"""Daily whole-assortment Kaspi market recovery monitoring."""
from alembic import op
import sqlalchemy as sa

revision: str = "20261020_0052"
down_revision: str | None = "20261020_0051"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("market_recovery_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rise_percent", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("city_id", sa.String(32), nullable=False, server_default="196220100"),
        sa.Column("zone_id", sa.String(64), nullable=False, server_default="Magnum_ZONE1"),
        sa.Column("catalog_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", name="uq_market_recovery_settings_workspace"))
    op.create_index("ix_market_recovery_settings_workspace_id", "market_recovery_settings", ["workspace_id"])
    op.create_table("market_recovery_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("kaspi_product_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("kaspi_url", sa.Text(), nullable=False),
        sa.Column("baseline_price", sa.Numeric(18, 2), nullable=True),
        sa.Column("current_price", sa.Numeric(18, 2), nullable=True),
        sa.Column("seller_count", sa.Integer(), nullable=True),
        sa.Column("sellers_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notified_band", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("completed_token", sa.String(64), nullable=True),
        sa.UniqueConstraint("workspace_id", "kaspi_product_id", name="uq_market_recovery_card"))
    op.create_index("ix_market_recovery_cards_workspace_id", "market_recovery_cards", ["workspace_id"])
    op.create_index("ix_market_recovery_due", "market_recovery_cards", ["workspace_id", "next_check_at", "lease_until"])
    if op.get_bind().dialect.name == "postgresql":
        for table in ("market_recovery_settings", "market_recovery_cards"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.drop_table("market_recovery_cards")
    op.drop_table("market_recovery_settings")
