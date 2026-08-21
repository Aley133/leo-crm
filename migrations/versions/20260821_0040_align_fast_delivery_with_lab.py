"""Align Fast Dumping delivery threshold with the proven lab rule.

Revision ID: 20260821_0040
Revises: 20260820_0039
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260821_0040"
down_revision: str | None = "20260820_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The localhost lab was validated with a three-day advantage. Existing CRM
    # policies were created with the old five-day default, so a competitor four
    # days slower was still treated as the price leader. Move those legacy
    # default-valued policies to the proven threshold and make 3 the DB default.
    op.alter_column(
        "fast_dumping_policies",
        "delivery_advantage_days",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="3",
    )
    op.execute(
        sa.text(
            "UPDATE fast_dumping_policies "
            "SET delivery_advantage_days = 3 "
            "WHERE delivery_advantage_days = 5"
        )
    )


def downgrade() -> None:
    # Do not rewrite policy values: after upgrade a user may deliberately choose
    # 3 days. Only restore the historical database default.
    op.alter_column(
        "fast_dumping_policies",
        "delivery_advantage_days",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="5",
    )
