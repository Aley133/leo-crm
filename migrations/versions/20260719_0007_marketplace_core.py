"""Add marketplace core schema.

Revision ID: 20260719_0007
Revises: 20260719_0006
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0007"
down_revision: str | None = "20260719_0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "marketplace_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_account_id", name="uq_marketplace_account_provider_external"
        ),
    )
    op.create_index("ix_marketplace_accounts_provider", "marketplace_accounts", ["provider"])

    op.create_table(
        "marketplace_import_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_marketplace_import_executions_account",
        "marketplace_import_executions",
        ["marketplace_account_id"],
    )
    op.create_index(
        "ix_marketplace_import_executions_status",
        "marketplace_import_executions",
        ["status"],
    )

    op.create_table(
        "marketplace_import_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("stream_name", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_account_id", "stream_name", name="uq_marketplace_checkpoint_account_stream"
        ),
    )
    op.create_index(
        "ix_marketplace_import_checkpoints_account",
        "marketplace_import_checkpoints",
        ["marketplace_account_id"],
    )

    op.create_table(
        "marketplace_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("external_order_id", sa.String(length=128), nullable=False),
        sa.Column("external_code", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_account_id", "external_order_id", name="uq_marketplace_order_account_external"
        ),
    )
    op.create_index("ix_marketplace_orders_account", "marketplace_orders", ["marketplace_account_id"])
    op.create_index("ix_marketplace_orders_external_code", "marketplace_orders", ["external_code"])
    op.create_index("ix_marketplace_orders_status", "marketplace_orders", ["status"])
    op.create_index("ix_marketplace_orders_ordered_at", "marketplace_orders", ["ordered_at"])

    op.create_table(
        "marketplace_order_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_order_id", sa.Integer(), nullable=False),
        sa.Column("external_line_id", sa.String(length=128), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("external_product_id", sa.String(length=128), nullable=True),
        sa.Column("merchant_sku", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_order_id"], ["marketplace_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_order_id", "external_line_id", name="uq_marketplace_order_line_external"
        ),
    )
    op.create_index("ix_marketplace_order_lines_order", "marketplace_order_lines", ["marketplace_order_id"])
    op.create_index("ix_marketplace_order_lines_product", "marketplace_order_lines", ["product_id"])
    op.create_index("ix_marketplace_order_lines_external_product", "marketplace_order_lines", ["external_product_id"])
    op.create_index("ix_marketplace_order_lines_merchant_sku", "marketplace_order_lines", ["merchant_sku"])

    op.create_table(
        "marketplace_order_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_order_id", sa.Integer(), nullable=False),
        sa.Column("source_event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_order_id"], ["marketplace_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_order_id", "source_event_key", name="uq_marketplace_order_event_source_key"
        ),
    )
    op.create_index("ix_marketplace_order_events_order", "marketplace_order_events", ["marketplace_order_id"])
    op.create_index("ix_marketplace_order_events_type", "marketplace_order_events", ["event_type"])
    op.create_index("ix_marketplace_order_events_occurred_at", "marketplace_order_events", ["occurred_at"])

    op.create_table(
        "marketplace_raw_payloads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("import_execution_id", sa.Uuid(), nullable=True),
        sa.Column("payload_type", sa.String(length=64), nullable=False),
        sa.Column("external_object_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["import_execution_id"], ["marketplace_import_executions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_account_id",
            "payload_type",
            "external_object_id",
            "content_hash",
            name="uq_marketplace_raw_payload_identity",
        ),
    )
    op.create_index("ix_marketplace_raw_payloads_account", "marketplace_raw_payloads", ["marketplace_account_id"])
    op.create_index("ix_marketplace_raw_payloads_execution", "marketplace_raw_payloads", ["import_execution_id"])
    op.create_index("ix_marketplace_raw_payloads_type", "marketplace_raw_payloads", ["payload_type"])
    op.create_index("ix_marketplace_raw_payloads_external_object", "marketplace_raw_payloads", ["external_object_id"])


def downgrade() -> None:
    op.drop_table("marketplace_raw_payloads")
    op.drop_table("marketplace_order_events")
    op.drop_table("marketplace_order_lines")
    op.drop_table("marketplace_orders")
    op.drop_table("marketplace_import_checkpoints")
    op.drop_table("marketplace_import_executions")
    op.drop_table("marketplace_accounts")