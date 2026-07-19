"""Add marketplace listing resolution history.

Revision ID: 20260719_0012
Revises: 20260719_0011
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0012"
down_revision: str | None = "20260719_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketplace_listing_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_listing_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("previous_product_id", sa.Integer(), nullable=True),
        sa.Column("current_product_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_listing_id"],
            ["marketplace_listings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_marketplace_listing_events_marketplace_listing_id",
        "marketplace_listing_events",
        ["marketplace_listing_id"],
    )
    op.create_index(
        "ix_marketplace_listing_events_event_type",
        "marketplace_listing_events",
        ["event_type"],
    )
    op.create_index(
        "ix_marketplace_listing_events_occurred_at",
        "marketplace_listing_events",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("marketplace_listing_events")
