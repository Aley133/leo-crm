"""Add the isolated realtime fast-dumping runtime.

Revision ID: 20260812_0032
Revises: 20260811_0031
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_0032"
down_revision: str | None = "20260811_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fast_dumping_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("minimum_profit_kzt", sa.Numeric(18, 2), nullable=False, server_default="1000"),
        sa.Column("undercut_step_kzt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("allow_price_raise", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_undercut_gap_percent", sa.Numeric(7, 2), nullable=False, server_default="35"),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("city_id", sa.String(length=32), nullable=False, server_default="750000000"),
        sa.Column("zone_id", sa.String(length=64), nullable=False, server_default="Magnum_ZONE1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_id",
            "product_id",
            name="uq_fast_dumping_policy_workspace_product",
        ),
    )
    op.create_index("ix_fast_dumping_policies_workspace_id", "fast_dumping_policies", ["workspace_id"])
    op.create_index("ix_fast_dumping_policies_product_id", "fast_dumping_policies", ["product_id"])

    op.create_table(
        "fast_dumping_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "policy_id",
            sa.Integer(),
            sa.ForeignKey("fast_dumping_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=48), nullable=False, server_default="idle"),
        sa.Column("decision_status", sa.String(length=48), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_cost_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("inventory_on_hand", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_floor_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("own_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("competitor_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("competitor_name", sa.String(length=255), nullable=True),
        sa.Column("target_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("desired_stock_count", sa.Integer(), nullable=True),
        sa.Column("own_position", sa.Integer(), nullable=True),
        sa.Column("seller_count", sa.Integer(), nullable=True),
        sa.Column("product_url", sa.Text(), nullable=True),
        sa.Column("product_model", sa.String(length=500), nullable=True),
        sa.Column("page_visible_price_kzt", sa.Numeric(18, 2), nullable=True),
        sa.Column("market_context_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("market_context_reason", sa.Text(), nullable=True),
        sa.Column("offers_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("offers_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_job_id", sa.Integer(), nullable=True),
        sa.Column("automatic_writes_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("last_operation_id", sa.String(length=255), nullable=True),
        sa.Column("last_agent_id", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_id",
            "product_id",
            name="uq_fast_dumping_state_workspace_product",
        ),
    )
    op.create_index("ix_fast_dumping_states_workspace_id", "fast_dumping_states", ["workspace_id"])
    op.create_index("ix_fast_dumping_states_policy_id", "fast_dumping_states", ["policy_id"])
    op.create_index("ix_fast_dumping_states_product_id", "fast_dumping_states", ["product_id"])
    op.create_index("ix_fast_dumping_states_status", "fast_dumping_states", ["status"])
    op.create_index("ix_fast_dumping_states_decision_status", "fast_dumping_states", ["decision_status"])
    op.create_index("ix_fast_dumping_states_active_job_id", "fast_dumping_states", ["active_job_id"])
    op.create_index("ix_fast_dumping_states_next_scan_at", "fast_dumping_states", ["next_scan_at"])
    op.create_index(
        "ix_fast_dumping_states_workspace_status_id",
        "fast_dumping_states",
        ["workspace_id", "status", "id"],
    )
    op.create_index(
        "ix_fast_dumping_states_workspace_due_id",
        "fast_dumping_states",
        ["workspace_id", "next_scan_at", "id"],
    )

    op.create_table(
        "fast_dumping_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "policy_id",
            sa.Integer(),
            sa.ForeignKey("fast_dumping_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=True),
        sa.Column("agent_id", sa.String(length=255), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("apply_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("decision_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("write_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_fast_dumping_jobs_workspace_id", "fast_dumping_jobs", ["workspace_id"])
    op.create_index("ix_fast_dumping_jobs_policy_id", "fast_dumping_jobs", ["policy_id"])
    op.create_index("ix_fast_dumping_jobs_product_id", "fast_dumping_jobs", ["product_id"])
    op.create_index("ix_fast_dumping_jobs_status", "fast_dumping_jobs", ["status"])
    op.create_index("ix_fast_dumping_jobs_lease_until", "fast_dumping_jobs", ["lease_until"])
    op.create_index("ix_fast_dumping_jobs_created_at", "fast_dumping_jobs", ["created_at"])
    op.create_index(
        "ix_fast_dumping_jobs_workspace_status_id",
        "fast_dumping_jobs",
        ["workspace_id", "status", "id"],
    )
    op.create_index(
        "ix_fast_dumping_jobs_product_status_id",
        "fast_dumping_jobs",
        ["product_id", "status", "id"],
    )


def downgrade() -> None:
    op.drop_table("fast_dumping_jobs")
    op.drop_table("fast_dumping_states")
    op.drop_table("fast_dumping_policies")
