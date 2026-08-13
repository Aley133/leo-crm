"""Add delivery-aware Fast Dumping thresholds.

Revision ID: 20260813_0034
Revises: 20260813_0033
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_0034"
down_revision: str | None = "20260813_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fast_dumping_policies",
        sa.Column(
            "delivery_price_premium_kzt",
            sa.Integer(),
            nullable=False,
            server_default="500",
        ),
    )
    op.add_column(
        "fast_dumping_policies",
        sa.Column(
            "delivery_advantage_days",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )


def downgrade() -> None:
    op.drop_column("fast_dumping_policies", "delivery_advantage_days")
    op.drop_column("fast_dumping_policies", "delivery_price_premium_kzt")
