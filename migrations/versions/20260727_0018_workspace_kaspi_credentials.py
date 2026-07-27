"""add workspace Kaspi credential storage

Revision ID: 20260727_0018
Revises: 20260727_0017
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260727_0018"
down_revision: str | None = "20260727_0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "kaspi_account_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("marketplace_account_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", sa.String(length=128), nullable=False),
        sa.Column("api_token_encrypted", sa.Text(), nullable=False),
        sa.Column("encryption_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["marketplace_account_id"], ["marketplace_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("marketplace_account_id", name="uq_kaspi_account_credentials_account"),
        sa.UniqueConstraint("workspace_id", name="uq_kaspi_account_credentials_workspace"),
    )
    op.create_index(
        "ix_kaspi_account_credentials_marketplace_account_id",
        "kaspi_account_credentials",
        ["marketplace_account_id"],
    )
    op.create_index(
        "ix_kaspi_account_credentials_workspace_id",
        "kaspi_account_credentials",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kaspi_account_credentials_workspace_id",
        table_name="kaspi_account_credentials",
    )
    op.drop_index(
        "ix_kaspi_account_credentials_marketplace_account_id",
        table_name="kaspi_account_credentials",
    )
    op.drop_table("kaspi_account_credentials")
