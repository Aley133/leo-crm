"""Add marketplace listing identity schema.

Revision ID: 20260719_0011
Revises: 20260719_0010
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0011"
down_revision: str | None = "20260719_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketplace_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("identity_kind", sa.String(length=32), nullable=False),
        sa.Column("identity_key", sa.String(length=300), nullable=False),
        sa.Column("merchant_sku", sa.String(length=128), nullable=True),
        sa.Column("external_product_id", sa.String(length=128), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_account_id"],
            ["marketplace_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_account_id",
            "identity_key",
            name="uq_marketplace_listing_account_identity",
        ),
    )
    op.create_index(
        "ix_marketplace_listings_marketplace_account_id",
        "marketplace_listings",
        ["marketplace_account_id"],
    )
    op.create_index(
        "ix_marketplace_listings_identity_kind",
        "marketplace_listings",
        ["identity_kind"],
    )
    op.create_index(
        "ix_marketplace_listings_merchant_sku",
        "marketplace_listings",
        ["merchant_sku"],
    )
    op.create_index(
        "ix_marketplace_listings_external_product_id",
        "marketplace_listings",
        ["external_product_id"],
    )
    op.create_index(
        "ix_marketplace_listings_product_id",
        "marketplace_listings",
        ["product_id"],
    )
    op.create_index(
        "ix_marketplace_listings_status",
        "marketplace_listings",
        ["status"],
    )

    op.create_table(
        "marketplace_listing_issues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("marketplace_order_line_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title_snapshot", sa.String(length=500), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_order_line_id"],
            ["marketplace_order_lines.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "marketplace_order_line_id",
            name="uq_marketplace_listing_issue_order_line",
        ),
    )
    op.create_index(
        "ix_marketplace_listing_issues_marketplace_order_line_id",
        "marketplace_listing_issues",
        ["marketplace_order_line_id"],
    )
    op.create_index(
        "ix_marketplace_listing_issues_reason",
        "marketplace_listing_issues",
        ["reason"],
    )
    op.create_index(
        "ix_marketplace_listing_issues_status",
        "marketplace_listing_issues",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("marketplace_listing_issues")
    op.drop_table("marketplace_listings")
