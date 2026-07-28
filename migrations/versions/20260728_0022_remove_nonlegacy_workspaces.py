"""remove all non-legacy workspace data

Revision ID: 20260728_0022
Revises: 20260728_0021

This migration intentionally restores the production database to the single
BARWORK workspace. The user explicitly approved permanent removal of the
experimental LeoXpress workspace and its data.
"""

from alembic import op


revision: str = "20260728_0022"
down_revision: str | None = "20260728_0021"
branch_labels: str | None = None
depends_on: str | None = None

LEGACY_WORKSPACE_ID = 1


def upgrade() -> None:
    # Capture the experimental account/order graph through subqueries and remove
    # the most deeply dependent business records first. Purchase requests use a
    # RESTRICT foreign key to marketplace_orders, so they must be removed before
    # the orders themselves. Their lines, receipts and events cascade.
    op.execute(
        "DELETE FROM purchase_requests "
        "WHERE marketplace_order_id IN ("
        "  SELECT mo.id FROM marketplace_orders mo "
        "  JOIN marketplace_accounts ma ON ma.id = mo.marketplace_account_id "
        "  WHERE ma.workspace_id <> 1"
        ")"
    )

    # Order lines/events are CASCADE children of marketplace_orders. Inventory
    # allocations linked to those lines also cascade. Removing orders explicitly
    # avoids relying on the account FK, which is RESTRICT in production.
    op.execute(
        "DELETE FROM marketplace_orders "
        "WHERE marketplace_account_id IN ("
        "  SELECT id FROM marketplace_accounts WHERE workspace_id <> 1"
        ")"
    )

    # Session/authentication records.
    op.execute(
        "DELETE FROM user_sessions "
        "WHERE user_id IN (SELECT id FROM app_users WHERE workspace_id <> 1)"
    )

    # Credentials and XML feeds owned directly by experimental workspaces.
    op.execute("DELETE FROM kaspi_account_credentials WHERE workspace_id <> 1")
    op.execute("DELETE FROM kaspi_xml_feeds WHERE workspace_id <> 1")

    # Imports, checkpoints, raw payloads and other account-owned rows are CASCADE
    # children. Orders have already been removed explicitly above.
    op.execute("DELETE FROM marketplace_accounts WHERE workspace_id <> 1")

    # Product-owned monitoring, supplier, FIFO and dumping records use CASCADE or
    # SET NULL in the existing schema.
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
