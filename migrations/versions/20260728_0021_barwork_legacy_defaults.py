"""keep pre-session BARWORK code compatible with workspace-shaped database

Revision ID: 20260728_0021
Revises: 20260727_0020
"""

from alembic import op

revision: str = "20260728_0021"
down_revision: str | None = "20260727_0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # The restored c28c943 application does not send workspace_id. Production
    # remains a single BARWORK tenant, represented by legacy workspace id=1.
    op.execute("ALTER TABLE marketplace_accounts ALTER COLUMN workspace_id SET DEFAULT 1")
    op.execute("ALTER TABLE products ALTER COLUMN workspace_id SET DEFAULT 1")
    op.execute("ALTER TABLE kaspi_xml_feeds ALTER COLUMN workspace_id SET DEFAULT 1")


def downgrade() -> None:
    op.execute("ALTER TABLE kaspi_xml_feeds ALTER COLUMN workspace_id DROP DEFAULT")
    op.execute("ALTER TABLE products ALTER COLUMN workspace_id DROP DEFAULT")
    op.execute("ALTER TABLE marketplace_accounts ALTER COLUMN workspace_id DROP DEFAULT")
