"""add product price alert opt-in

Revision ID: 20260729_0024
Revises: 20260728_0023
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260729_0024"
down_revision: str | None = "20260728_0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "sudden_price_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "sudden_price_alert_enabled")
