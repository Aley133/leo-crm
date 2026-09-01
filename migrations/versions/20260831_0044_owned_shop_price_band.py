"""Add the cooperative owned-shop price band to Fast Dumping.

Revision ID: 20260831_0044
Revises: 20260826_0043
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0044"
down_revision: str | None = "20260826_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fast_dumping_policies",
        sa.Column(
            "owned_price_band_kzt",
            sa.Integer(),
            nullable=False,
            server_default="200",
        ),
    )


def downgrade() -> None:
    op.drop_column("fast_dumping_policies", "owned_price_band_kzt")
