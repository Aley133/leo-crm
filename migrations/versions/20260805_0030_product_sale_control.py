"""Add durable per-product sale control.

Revision ID: 20260805_0030
Revises: 20260803_0029
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0030"
down_revision: str | None = "20260803_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "sale_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index(
        "ix_products_sale_enabled",
        "products",
        ["sale_enabled"],
    )
    op.add_column(
        "products",
        sa.Column(
            "sale_state_overridden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "sale_state_overridden")
    op.drop_index("ix_products_sale_enabled", table_name="products")
    op.drop_column("products", "sale_enabled")
