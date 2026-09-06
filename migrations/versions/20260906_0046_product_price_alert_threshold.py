"""Add a selectable per-product price-drop alert threshold.

Revision ID: 20260906_0046
Revises: 20260901_0045
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0046"
down_revision: str | None = "20260901_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "sudden_price_alert_threshold_percent",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "sudden_price_alert_threshold_percent")
