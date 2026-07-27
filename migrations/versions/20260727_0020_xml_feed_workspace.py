"""assign Kaspi XML feeds to workspaces

Revision ID: 20260727_0020
Revises: 20260727_0019
"""

from alembic import op
import sqlalchemy as sa

revision = "20260727_0020"
down_revision = "20260727_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kaspi_xml_feeds",
        sa.Column("workspace_id", sa.Integer(), nullable=True),
    )
    op.execute("UPDATE kaspi_xml_feeds SET workspace_id = 1 WHERE workspace_id IS NULL")
    op.alter_column("kaspi_xml_feeds", "workspace_id", nullable=False)
    op.create_foreign_key(
        "fk_kaspi_xml_feeds_workspace_id",
        "kaspi_xml_feeds",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_kaspi_xml_feeds_workspace_id",
        "kaspi_xml_feeds",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kaspi_xml_feeds_workspace_id", table_name="kaspi_xml_feeds")
    op.drop_constraint(
        "fk_kaspi_xml_feeds_workspace_id",
        "kaspi_xml_feeds",
        type_="foreignkey",
    )
    op.drop_column("kaspi_xml_feeds", "workspace_id")
