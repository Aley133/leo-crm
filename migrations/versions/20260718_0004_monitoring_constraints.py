"""Enforce monitoring domain constraints.

Revision ID: 20260718_0004
Revises: 20260718_0003
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0004"
down_revision: str | None = "20260718_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_product_bindings_status",
        "product_bindings",
        "status IN ('candidate','confirmed','active','degraded','disabled','rejected')",
    )
    op.create_check_constraint(
        "ck_product_bindings_decision_source",
        "product_bindings",
        "decision_source IN ('automatic','manual','imported')",
    )
    op.create_check_constraint(
        "ck_product_bindings_confidence_score",
        "product_bindings",
        "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100)",
    )
    op.create_check_constraint(
        "ck_product_bindings_priority",
        "product_bindings",
        "priority >= 0",
    )
    op.create_check_constraint(
        "ck_monitor_targets_status",
        "monitor_targets",
        "status IN ('active','paused','degraded','manual_review','disabled')",
    )
    op.create_check_constraint(
        "ck_monitor_targets_interval_seconds",
        "monitor_targets",
        "interval_seconds >= 60",
    )
    op.create_check_constraint(
        "ck_monitor_targets_failures",
        "monitor_targets",
        "consecutive_failures >= 0",
    )
    op.create_check_constraint(
        "ck_monitor_targets_shard",
        "monitor_targets",
        "shard >= 0 AND shard < 100",
    )
    op.create_check_constraint(
        "ck_monitor_attempts_outcome",
        "monitor_attempts",
        "outcome IN ('success','timeout','rate_limited','captcha','blocked','auth_required','not_found','parse_error','network_error','internal_error')",
    )
    op.create_check_constraint(
        "ck_monitor_attempts_duration",
        "monitor_attempts",
        "duration_ms IS NULL OR duration_ms >= 0",
    )
    op.create_check_constraint(
        "ck_offer_state_non_negative",
        "supplier_offer_states",
        "(price IS NULL OR price >= 0) AND (old_price IS NULL OR old_price >= 0) AND (stock IS NULL OR stock >= 0) AND (delivery_days IS NULL OR delivery_days >= 0) AND version >= 1",
    )
    op.create_check_constraint(
        "ck_offer_observation_non_negative",
        "supplier_offer_observations",
        "(price IS NULL OR price >= 0) AND (old_price IS NULL OR old_price >= 0) AND (stock IS NULL OR stock >= 0) AND (delivery_days IS NULL OR delivery_days >= 0)",
    )
    op.create_check_constraint(
        "ck_source_health_status",
        "source_health",
        "status IN ('healthy','degraded','rate_limited','captcha_required','blocked','auth_required','disabled')",
    )
    op.create_check_constraint(
        "ck_source_health_failures",
        "source_health",
        "consecutive_failures >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_source_health_failures", "source_health", type_="check")
    op.drop_constraint("ck_source_health_status", "source_health", type_="check")
    op.drop_constraint("ck_offer_observation_non_negative", "supplier_offer_observations", type_="check")
    op.drop_constraint("ck_offer_state_non_negative", "supplier_offer_states", type_="check")
    op.drop_constraint("ck_monitor_attempts_duration", "monitor_attempts", type_="check")
    op.drop_constraint("ck_monitor_attempts_outcome", "monitor_attempts", type_="check")
    op.drop_constraint("ck_monitor_targets_shard", "monitor_targets", type_="check")
    op.drop_constraint("ck_monitor_targets_failures", "monitor_targets", type_="check")
    op.drop_constraint("ck_monitor_targets_interval_seconds", "monitor_targets", type_="check")
    op.drop_constraint("ck_monitor_targets_status", "monitor_targets", type_="check")
    op.drop_constraint("ck_product_bindings_priority", "product_bindings", type_="check")
    op.drop_constraint("ck_product_bindings_confidence_score", "product_bindings", type_="check")
    op.drop_constraint("ck_product_bindings_decision_source", "product_bindings", type_="check")
    op.drop_constraint("ck_product_bindings_status", "product_bindings", type_="check")
