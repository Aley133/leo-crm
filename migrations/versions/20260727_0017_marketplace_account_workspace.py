"""assign marketplace accounts to workspaces

Revision ID: 20260727_0017
Revises: 20260727_0016
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260727_0017"
down_revision: str | None = "20260727_0016"
branch_labels: str | None = None
depends_on: str | None = None

LEGACY_WORKSPACE_ID = 1


def upgrade() -> None:
    with op.batch_alter_table("marketplace_accounts") as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=False, server_default=str(LEGACY_WORKSPACE_ID)))
        batch_op.create_foreign_key("fk_marketplace_accounts_workspace_id_workspaces", "workspaces", ["workspace_id"], ["id"], ondelete="RESTRICT")
        batch_op.create_index("ix_marketplace_accounts_workspace_id", ["workspace_id"])
        batch_op.drop_constraint("uq_marketplace_account_provider_external", type_="unique")
        batch_op.create_unique_constraint("uq_marketplace_account_workspace_provider_external", ["workspace_id", "provider", "external_account_id"])
    with op.batch_alter_table("marketplace_accounts") as batch_op:
        batch_op.alter_column("workspace_id", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("marketplace_accounts") as batch_op:
        batch_op.drop_constraint("uq_marketplace_account_workspace_provider_external", type_="unique")
        batch_op.create_unique_constraint("uq_marketplace_account_provider_external", ["provider", "external_account_id"])
        batch_op.drop_index("ix_marketplace_accounts_workspace_id")
        batch_op.drop_constraint("fk_marketplace_accounts_workspace_id_workspaces", type_="foreignkey")
        batch_op.drop_column("workspace_id")
