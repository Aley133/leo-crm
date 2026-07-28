"""add inventory arrival state

Revision ID: 20260728_0023
Revises: 20260728_0022
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260728_0023"
down_revision: str | None = "20260728_0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_batches",
        sa.Column("is_received", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(
        "ix_inventory_batches_is_received",
        "inventory_batches",
        ["is_received"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_batches_is_received", table_name="inventory_batches")
    op.drop_column("inventory_batches", "is_received")
