"""add supplier offer currency

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier_offer_states", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("supplier_offer_observations", sa.Column("currency", sa.String(length=3), nullable=True))


def downgrade() -> None:
    op.drop_column("supplier_offer_observations", "currency")
    op.drop_column("supplier_offer_states", "currency")
