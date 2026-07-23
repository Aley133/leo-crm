"""add FIFO inventory batches

Revision ID: 20260723_0013
Revises: 20260719_0012
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260723_0013"
down_revision: str | None = "20260719_0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity_received", sa.Integer(), nullable=False),
        sa.Column("quantity_remaining", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity_received > 0", name="ck_inventory_batch_received_positive"),
        sa.CheckConstraint(
            "quantity_remaining >= 0 AND quantity_remaining <= quantity_received",
            name="ck_inventory_batch_remaining_range",
        ),
        sa.CheckConstraint("unit_cost >= 0", name="ck_inventory_batch_unit_cost_nonnegative"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_batches_product_id", "inventory_batches", ["product_id"])
    op.create_index("ix_inventory_batches_received_at", "inventory_batches", ["received_at"])

    op.create_table(
        "inventory_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inventory_batch_id", sa.Integer(), nullable=False),
        sa.Column("marketplace_order_line_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 2), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_inventory_allocation_quantity_positive"),
        sa.CheckConstraint("unit_cost >= 0", name="ck_inventory_allocation_unit_cost_nonnegative"),
        sa.ForeignKeyConstraint(["inventory_batch_id"], ["inventory_batches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["marketplace_order_line_id"], ["marketplace_order_lines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inventory_batch_id",
            "marketplace_order_line_id",
            name="uq_inventory_allocation_batch_order_line",
        ),
    )
    op.create_index(
        "ix_inventory_allocations_inventory_batch_id",
        "inventory_allocations",
        ["inventory_batch_id"],
    )
    op.create_index(
        "ix_inventory_allocations_marketplace_order_line_id",
        "inventory_allocations",
        ["marketplace_order_line_id"],
    )
    op.create_index(
        "ix_inventory_allocations_allocated_at",
        "inventory_allocations",
        ["allocated_at"],
    )


def downgrade() -> None:
    op.drop_table("inventory_allocations")
    op.drop_table("inventory_batches")
