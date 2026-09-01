"""Persist the owned-shop cycle anchor when no external seller exists.

Revision ID: 20260901_0045
Revises: 20260831_0044
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0045"
down_revision: str | None = "20260831_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fast_dumping_states",
        sa.Column(
            "owned_cycle_anchor_price_kzt",
            sa.Numeric(18, 2),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("fast_dumping_states", "owned_cycle_anchor_price_kzt")
