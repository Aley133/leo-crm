"""add production batch workflow

Revision ID: 20260729_0025
Revises: 20260729_0024
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0025"
down_revision: str | None = "20260729_0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_batches",
        sa.Column(
            "batch_type",
            sa.String(length=32),
            nullable=False,
            server_default="purchase",
        ),
    )
    op.create_check_constraint(
        "ck_inventory_batch_type",
        "inventory_batches",
        "batch_type IN ('purchase', 'production')",
    )
    op.create_index(
        "ix_inventory_batches_batch_type",
        "inventory_batches",
        ["batch_type"],
        unique=False,
    )

    # Legacy production batches were stored as physically received inventory
    # and therefore moved covered preorders to packaging immediately. Convert
    # only explicitly named production sources and remove those automatic
    # allocations so every order must be confirmed with "Изготовлено".
    op.execute(
        """
        UPDATE inventory_batches
        SET batch_type = 'production'
        WHERE lower(trim(coalesce(source_name, ''))) LIKE '%производ%'
           OR lower(trim(coalesce(source_name, ''))) = 'production'
        """
    )
    op.execute(
        """
        DELETE FROM inventory_allocations
        WHERE inventory_batch_id IN (
            SELECT id
            FROM inventory_batches
            WHERE batch_type = 'production'
        )
        """
    )
    op.execute(
        """
        UPDATE inventory_batches
        SET is_received = false,
            quantity_remaining = 0
        WHERE batch_type = 'production'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_batches_batch_type", table_name="inventory_batches")
    op.drop_constraint(
        "ck_inventory_batch_type",
        "inventory_batches",
        type_="check",
    )
    op.drop_column("inventory_batches", "batch_type")
