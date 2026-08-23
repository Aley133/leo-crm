"""Add supplier-preorder target position to Fast Dumping.

Revision ID: 20260823_0041
Revises: 20260821_0040
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260823_0041"
down_revision: str | None = "20260821_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fast_dumping_policies",
        sa.Column(
            "preorder_target_position",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
    )


def downgrade() -> None:
    op.drop_column("fast_dumping_policies", "preorder_target_position")
