from __future__ import annotations

"""Idempotently repair browser-agent, monitoring and pricing schema drift.

This deployment safeguard covers installations whose Alembic history was stamped
without later browser-agent, monitoring-currency or pricing-engine changes. It
creates only missing ORM tables/indexes, adds only known nullable columns, verifies
required columns, and never removes data.
"""

from sqlalchemy import inspect, text

from backend.app import monitoring  # noqa: F401  # register monitoring metadata
from backend.app import models  # noqa: F401  # register product metadata
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.db import engine
from backend.app.pricing_models import FxRateSnapshot, PriceCalculation, PricingPolicy


_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "browser_agent_jobs": {
        "id",
        "monitor_target_id",
        "supplier_product_id",
        "url",
        "status",
        "lease_owner",
        "lease_token",
        "lease_until",
        "result_payload",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "finished_at",
    },
    "supplier_offer_states": {
        "id",
        "supplier_product_id",
        "price",
        "old_price",
        "currency",
        "available",
        "stock",
        "delivery_days",
        "seller",
        "fingerprint",
        "adapter_schema_version",
        "observed_at",
        "last_checked_at",
        "version",
        "updated_at",
    },
    "supplier_offer_observations": {
        "id",
        "supplier_product_id",
        "monitor_attempt_id",
        "price",
        "old_price",
        "currency",
        "available",
        "stock",
        "delivery_days",
        "seller",
        "fingerprint",
        "adapter_schema_version",
        "raw_metadata",
        "observed_at",
        "created_at",
    },
    "pricing_policies": {
        "id",
        "product_id",
        "enabled",
        "target_margin_pct",
        "marketplace_fee_pct",
        "payment_fee_pct",
        "delivery_cost_kzt",
        "fixed_cost_kzt",
        "minimum_price_kzt",
        "rounding_step_kzt",
        "created_at",
        "updated_at",
    },
    "fx_rate_snapshots": {
        "id",
        "base_currency",
        "quote_currency",
        "rate",
        "source",
        "observed_at",
        "created_at",
    },
    "price_calculations": {
        "id",
        "product_id",
        "pricing_policy_id",
        "supplier_offer_state_id",
        "fx_rate_snapshot_id",
        "status",
        "supplier_price",
        "supplier_currency",
        "fx_rate_to_kzt",
        "supplier_cost_kzt",
        "delivery_cost_kzt",
        "fixed_cost_kzt",
        "total_fee_pct",
        "target_margin_pct",
        "recommended_price_kzt",
        "explanation_json",
        "created_at",
    },
}


_SAFE_NULLABLE_COLUMN_REPAIRS: dict[str, dict[str, str]] = {
    "supplier_offer_states": {
        "currency": "VARCHAR(3)",
    },
    "supplier_offer_observations": {
        "currency": "VARCHAR(3)",
    },
}


def _repair_safe_nullable_columns() -> None:
    """Add known nullable columns that are safe on existing production rows."""
    inspector = inspect(engine)
    available_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table_name, columns in _SAFE_NULLABLE_COLUMN_REPAIRS.items():
            if table_name not in available_tables:
                continue
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name, sql_type in columns.items():
                if column_name in existing_columns:
                    continue
                connection.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN IF NOT EXISTS "{column_name}" {sql_type} NULL'
                    )
                )


def _verify_required_columns() -> None:
    inspector = inspect(engine)
    available_tables = set(inspector.get_table_names())

    for table_name, required_columns in _REQUIRED_COLUMNS.items():
        if table_name not in available_tables:
            raise RuntimeError(f"{table_name} was not created")

        columns = {
            column["name"]
            for column in inspector.get_columns(table_name)
        }
        missing = sorted(required_columns - columns)
        if missing:
            raise RuntimeError(
                f"{table_name} exists but is missing required columns: "
                + ", ".join(missing)
            )


def ensure_browser_agent_schema() -> None:
    # Create in foreign-key dependency order. checkfirst keeps repeated deploys safe.
    BrowserAgentJob.__table__.create(bind=engine, checkfirst=True)
    PricingPolicy.__table__.create(bind=engine, checkfirst=True)
    FxRateSnapshot.__table__.create(bind=engine, checkfirst=True)
    PriceCalculation.__table__.create(bind=engine, checkfirst=True)

    _repair_safe_nullable_columns()
    _verify_required_columns()
    print("browser-agent, monitoring currency and pricing schema are ready")


if __name__ == "__main__":
    ensure_browser_agent_schema()
