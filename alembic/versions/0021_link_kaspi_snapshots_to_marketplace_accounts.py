"""link Kaspi Seller snapshots to marketplace accounts

Revision ID: 0021
Revises: 0020
"""

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("kaspi_seller_order_snapshots")}
    if "marketplace_account_id" not in columns:
        op.add_column(
            "kaspi_seller_order_snapshots",
            sa.Column("marketplace_account_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_kaspi_seller_snapshots_marketplace_account",
            "kaspi_seller_order_snapshots",
            "marketplace_accounts",
            ["marketplace_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_kaspi_seller_order_snapshots_marketplace_account_id",
            "kaspi_seller_order_snapshots",
            ["marketplace_account_id"],
        )

    op.execute(
        sa.text(
            """
            UPDATE kaspi_seller_order_snapshots AS snapshots
            SET marketplace_account_id = accounts.id
            FROM marketplace_accounts AS accounts
            WHERE snapshots.marketplace_account_id IS NULL
              AND accounts.provider = 'kaspi'
              AND accounts.external_account_id = snapshots.merchant_id
            """
        )
    )


def downgrade() -> None:
    op.drop_column("kaspi_seller_order_snapshots", "marketplace_account_id")