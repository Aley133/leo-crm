"""Bound local-agent photo backfill attempts.

Revision ID: 20260820_0038
Revises: 20260819_0037
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260820_0038"
down_revision: str | None = "20260819_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "image_backfill_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "image_backfill_attempts")
