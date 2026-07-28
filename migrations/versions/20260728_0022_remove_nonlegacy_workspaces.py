"""remove all non-legacy workspace data

Revision ID: 20260728_0022
Revises: 20260728_0021

This migration intentionally restores the production database to the single
BARWORK workspace.  The user explicitly approved permanent removal of the
experimental LeoXpress workspace and its data.
"""

from alembic import op


revision: str = "20260728_0022"
down_revision: str | None = "20260728_0021"
branch_labels: str | None = None
depends_on: str | None = None

LEGACY_WORKSPACE_ID = 1


def upgrade() -> None:
    # Remove session/authentication records first. user_sessions are also
    # protected by ON DELETE CASCADE, but the explicit delete keeps the cleanup
    # understandable and safe across schema variants.
    op.execute(
        "DELETE FROM user_sessions "
        "WHERE user_id IN (SELECT id FROM app_users WHERE workspace_id <> 1)"
    )

    # Remove Kaspi credentials and XML feeds owned by experimental workspaces.
    op.execute("DELETE FROM kaspi_account_credentials WHERE workspace_id <> 1")
    op.execute("DELETE FROM kaspi_xml_feeds WHERE workspace_id <> 1")

    # Deleting marketplace accounts cascades through imports, checkpoints,
    # raw payloads, orders, lines and events belonging to LeoXpress.
    op.execute("DELETE FROM marketplace_accounts WHERE workspace_id <> 1")

    # Product-owned records use CASCADE or SET NULL in the existing schema.
    # This removes LeoXpress products and their monitoring/FIFO/dumping links.
    op.execute("DELETE FROM products WHERE workspace_id <> 1")

    op.execute("DELETE FROM app_users WHERE workspace_id <> 1")
    op.execute("DELETE FROM workspaces WHERE id <> 1")

    # Keep the surviving workspace explicitly recognizable as BARWORK.
    op.execute(
        "UPDATE workspaces SET name = 'BARWORK', slug = 'barwork', is_active = true "
        "WHERE id = 1"
    )


def downgrade() -> None:
    # Deliberately irreversible: deleted LeoXpress credentials, orders and
    # products must not be recreated with fabricated values.
    pass
