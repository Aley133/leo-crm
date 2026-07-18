"""Monitoring contract and binding lifecycle.

Revision ID: 20260718_0003
Revises: 20260718_0002
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260718_0003"
down_revision: str | None = "20260718_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("product_bindings", sa.Column("status", sa.String(length=32), nullable=False, server_default="candidate"))
    op.add_column("product_bindings", sa.Column("decision_source", sa.String(length=32), nullable=False, server_default="manual"))
    op.add_column("product_bindings", sa.Column("priority", sa.Integer(), nullable=False, server_default="100"))
    op.add_column("product_bindings", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("product_bindings", sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("product_bindings", sa.Column("last_mismatch_reason", sa.String(length=1000), nullable=True))
    op.add_column("product_bindings", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_product_bindings_status", "product_bindings", ["status"])

    op.create_table(
        "monitor_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_binding_id", sa.Integer(), sa.ForeignKey("product_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shard", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_binding_id", name="uq_monitor_target_binding"),
        sa.UniqueConstraint("lease_token", name="uq_monitor_target_lease_token"),
    )
    op.create_index("ix_monitor_targets_binding_id", "monitor_targets", ["product_binding_id"])
    op.create_index("ix_monitor_targets_due", "monitor_targets", ["status", "next_check_at", "lease_until"])
    op.create_index("ix_monitor_targets_shard", "monitor_targets", ["shard"])

    op.create_table(
        "monitor_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("monitor_target_id", sa.Integer(), sa.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("adapter_code", sa.String(length=64), nullable=False),
        sa.Column("access_strategy", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_monitor_attempts_target_started", "monitor_attempts", ["monitor_target_id", sa.text("started_at DESC")])
    op.create_index("ix_monitor_attempts_outcome", "monitor_attempts", ["outcome"])
    op.create_index("ix_monitor_attempts_lease_token", "monitor_attempts", ["lease_token"])

    op.create_table(
        "supplier_offer_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_product_id", sa.Integer(), sa.ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=True),
        sa.Column("old_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column("seller", sa.String(length=500), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("adapter_schema_version", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("supplier_product_id", name="uq_supplier_offer_state_product"),
    )
    op.create_index("ix_supplier_offer_states_product_id", "supplier_offer_states", ["supplier_product_id"])
    op.create_index("ix_supplier_offer_states_fingerprint", "supplier_offer_states", ["fingerprint"])

    op.create_table(
        "supplier_offer_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_product_id", sa.Integer(), sa.ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("monitor_attempt_id", sa.Integer(), sa.ForeignKey("monitor_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("price", sa.Numeric(14, 2), nullable=True),
        sa.Column("old_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column("seller", sa.String(length=500), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("adapter_schema_version", sa.String(length=32), nullable=False),
        sa.Column("raw_metadata", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("supplier_product_id", "fingerprint", name="uq_supplier_observation_fingerprint"),
    )
    op.create_index("ix_supplier_observations_product_observed", "supplier_offer_observations", ["supplier_product_id", sa.text("observed_at DESC")])
    op.create_index("ix_supplier_observations_attempt_id", "supplier_offer_observations", ["monitor_attempt_id"])

    op.create_table(
        "source_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="healthy"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("supplier_id", name="uq_source_health_supplier"),
    )
    op.create_index("ix_source_health_supplier_id", "source_health", ["supplier_id"])
    op.create_index("ix_source_health_status", "source_health", ["status"])


def downgrade() -> None:
    op.drop_table("source_health")
    op.drop_table("supplier_offer_observations")
    op.drop_table("supplier_offer_states")
    op.drop_table("monitor_attempts")
    op.drop_table("monitor_targets")
    op.drop_index("ix_product_bindings_status", table_name="product_bindings")
    op.drop_column("product_bindings", "updated_at")
    op.drop_column("product_bindings", "last_mismatch_reason")
    op.drop_column("product_bindings", "last_validated_at")
    op.drop_column("product_bindings", "confirmed_at")
    op.drop_column("product_bindings", "priority")
    op.drop_column("product_bindings", "decision_source")
    op.drop_column("product_bindings", "status")
