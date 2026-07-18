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


def _create_check_if_missing(name: str, table: str, condition: str) -> None:
    """Create a PostgreSQL CHECK constraint only when it is absent.

    Production may contain a constraint created by an earlier interrupted
    deployment while alembic_version still points to the previous revision.
    Constraint existence is checked for the concrete table, not globally.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = '{name}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table}
                ADD CONSTRAINT {name} CHECK ({condition});
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    _create_check_if_missing(
        "ck_product_bindings_status",
        "product_bindings",
        "status IN ('candidate','confirmed','active','degraded','disabled','rejected')",
    )
    _create_check_if_missing(
        "ck_product_bindings_decision_source",
        "product_bindings",
        "decision_source IN ('automatic','manual','imported')",
    )
    _create_check_if_missing(
        "ck_product_bindings_confidence_score",
        "product_bindings",
        "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100)",
    )
    _create_check_if_missing(
        "ck_product_bindings_priority",
        "product_bindings",
        "priority >= 0",
    )
    _create_check_if_missing(
        "ck_monitor_targets_status",
        "monitor_targets",
        "status IN ('active','paused','degraded','manual_review','disabled')",
    )
    _create_check_if_missing(
        "ck_monitor_targets_interval_seconds",
        "monitor_targets",
        "interval_seconds >= 60",
    )
    _create_check_if_missing(
        "ck_monitor_targets_failures",
        "monitor_targets",
        "consecutive_failures >= 0",
    )
    _create_check_if_missing(
        "ck_monitor_targets_shard",
        "monitor_targets",
        "shard >= 0 AND shard < 100",
    )
    _create_check_if_missing(
        "ck_monitor_attempts_outcome",
        "monitor_attempts",
        "outcome IN ('success','timeout','rate_limited','captcha','blocked','auth_required','not_found','parse_error','network_error','internal_error')",
    )
    _create_check_if_missing(
        "ck_monitor_attempts_duration",
        "monitor_attempts",
        "duration_ms IS NULL OR duration_ms >= 0",
    )
    _create_check_if_missing(
        "ck_offer_state_non_negative",
        "supplier_offer_states",
        "(price IS NULL OR price >= 0) AND (old_price IS NULL OR old_price >= 0) AND (stock IS NULL OR stock >= 0) AND (delivery_days IS NULL OR delivery_days >= 0) AND version >= 1",
    )
    _create_check_if_missing(
        "ck_offer_observation_non_negative",
        "supplier_offer_observations",
        "(price IS NULL OR price >= 0) AND (old_price IS NULL OR old_price >= 0) AND (stock IS NULL OR stock >= 0) AND (delivery_days IS NULL OR delivery_days >= 0)",
    )
    _create_check_if_missing(
        "ck_source_health_status",
        "source_health",
        "status IN ('healthy','degraded','rate_limited','captcha_required','blocked','auth_required','disabled')",
    )
    _create_check_if_missing(
        "ck_source_health_failures",
        "source_health",
        "consecutive_failures >= 0",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE source_health DROP CONSTRAINT IF EXISTS ck_source_health_failures")
    op.execute("ALTER TABLE source_health DROP CONSTRAINT IF EXISTS ck_source_health_status")
    op.execute("ALTER TABLE supplier_offer_observations DROP CONSTRAINT IF EXISTS ck_offer_observation_non_negative")
    op.execute("ALTER TABLE supplier_offer_states DROP CONSTRAINT IF EXISTS ck_offer_state_non_negative")
    op.execute("ALTER TABLE monitor_attempts DROP CONSTRAINT IF EXISTS ck_monitor_attempts_duration")
    op.execute("ALTER TABLE monitor_attempts DROP CONSTRAINT IF EXISTS ck_monitor_attempts_outcome")
    op.execute("ALTER TABLE monitor_targets DROP CONSTRAINT IF EXISTS ck_monitor_targets_shard")
    op.execute("ALTER TABLE monitor_targets DROP CONSTRAINT IF EXISTS ck_monitor_targets_failures")
    op.execute("ALTER TABLE monitor_targets DROP CONSTRAINT IF EXISTS ck_monitor_targets_interval_seconds")
    op.execute("ALTER TABLE monitor_targets DROP CONSTRAINT IF EXISTS ck_monitor_targets_status")
    op.execute("ALTER TABLE product_bindings DROP CONSTRAINT IF EXISTS ck_product_bindings_priority")
    op.execute("ALTER TABLE product_bindings DROP CONSTRAINT IF EXISTS ck_product_bindings_confidence_score")
    op.execute("ALTER TABLE product_bindings DROP CONSTRAINT IF EXISTS ck_product_bindings_decision_source")
    op.execute("ALTER TABLE product_bindings DROP CONSTRAINT IF EXISTS ck_product_bindings_status")
