"""Add purchase lifecycle core schema.

Revision ID: 20260719_0010
Revises: 20260719_0009
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0010"
down_revision: str | None = "20260719_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("marketplace_order_id", sa.Integer(), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("expected_total", sa.Numeric(18, 2), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_order_id"], ["marketplace_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_requests_marketplace_order_id", "purchase_requests", ["marketplace_order_id"])
    op.create_index("ix_purchase_requests_origin", "purchase_requests", ["origin"])
    op.create_index("ix_purchase_requests_status", "purchase_requests", ["status"])

    op.create_table(
        "purchase_request_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_request_id", sa.Uuid(), nullable=False),
        sa.Column("marketplace_order_line_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("received_quantity", sa.Integer(), nullable=False),
        sa.Column("expected_unit_cost", sa.Numeric(18, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_purchase_request_line_quantity_positive"),
        sa.CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= quantity",
            name="ck_purchase_request_line_received_quantity_range",
        ),
        sa.ForeignKeyConstraint(["marketplace_order_line_id"], ["marketplace_order_lines.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_request_lines_purchase_request_id", "purchase_request_lines", ["purchase_request_id"])
    op.create_index("ix_purchase_request_lines_marketplace_order_line_id", "purchase_request_lines", ["marketplace_order_line_id"])
    op.create_index("ix_purchase_request_lines_product_id", "purchase_request_lines", ["product_id"])

    op.create_table(
        "purchase_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_request_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("current_status", sa.String(length=32), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_request_id",
            "idempotency_key",
            name="uq_purchase_event_request_idempotency",
        ),
    )
    op.create_index("ix_purchase_events_purchase_request_id", "purchase_events", ["purchase_request_id"])
    op.create_index("ix_purchase_events_event_type", "purchase_events", ["event_type"])
    op.create_index("ix_purchase_events_occurred_at", "purchase_events", ["occurred_at"])

    op.create_table(
        "purchase_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_request_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_number", sa.String(length=128), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_receipts_purchase_request_id", "purchase_receipts", ["purchase_request_id"])
    op.create_index("ix_purchase_receipts_received_at", "purchase_receipts", ["received_at"])

    op.create_table(
        "purchase_receipt_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_receipt_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_request_line_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 2), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_purchase_receipt_line_quantity_positive"),
        sa.ForeignKeyConstraint(["purchase_receipt_id"], ["purchase_receipts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["purchase_request_line_id"], ["purchase_request_lines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_receipt_id",
            "purchase_request_line_id",
            name="uq_purchase_receipt_line_identity",
        ),
    )
    op.create_index("ix_purchase_receipt_lines_purchase_receipt_id", "purchase_receipt_lines", ["purchase_receipt_id"])
    op.create_index("ix_purchase_receipt_lines_purchase_request_line_id", "purchase_receipt_lines", ["purchase_request_line_id"])


def downgrade() -> None:
    op.drop_table("purchase_receipt_lines")
    op.drop_table("purchase_receipts")
    op.drop_table("purchase_events")
    op.drop_table("purchase_request_lines")
    op.drop_table("purchase_requests")
