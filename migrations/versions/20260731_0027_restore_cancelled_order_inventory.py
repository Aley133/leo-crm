"""restore inventory allocated to cancelled orders

Revision ID: 20260731_0027
Revises: 20260731_0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "20260731_0027"
down_revision: str | None = "20260731_0026"
branch_labels: str | None = None
depends_on: str | None = None


def _restore_cancelled_order_allocations(connection) -> int:
    rows = connection.execute(
        sa.text(
            """
            SELECT
                ib.id AS batch_id,
                ib.quantity_received AS quantity_received,
                ib.quantity_remaining AS quantity_remaining,
                SUM(ia.quantity) AS released_quantity
            FROM inventory_allocations ia
            JOIN inventory_batches ib
              ON ib.id = ia.inventory_batch_id
            JOIN marketplace_order_lines mol
              ON mol.id = ia.marketplace_order_line_id
            JOIN marketplace_orders mo
              ON mo.id = mol.marketplace_order_id
            WHERE mo.status = 'cancelled'
              AND ib.batch_type = 'purchase'
            GROUP BY ib.id, ib.quantity_received, ib.quantity_remaining
            ORDER BY ib.id
            """
        )
    ).mappings().all()

    released_total = 0
    for row in rows:
        released_quantity = int(row["released_quantity"] or 0)
        if released_quantity <= 0:
            continue
        restored_remaining = int(row["quantity_remaining"]) + released_quantity
        if restored_remaining > int(row["quantity_received"]):
            raise RuntimeError(
                "cancelled order inventory repair exceeds received batch quantity"
            )
        connection.execute(
            sa.text(
                """
                UPDATE inventory_batches
                SET quantity_remaining = :quantity_remaining
                WHERE id = :batch_id
                """
            ),
            {
                "batch_id": int(row["batch_id"]),
                "quantity_remaining": restored_remaining,
            },
        )
        released_total += released_quantity

    if released_total:
        connection.execute(
            sa.text(
                """
                DELETE FROM inventory_allocations
                WHERE id IN (
                    SELECT ia.id
                    FROM inventory_allocations ia
                    JOIN inventory_batches ib
                      ON ib.id = ia.inventory_batch_id
                    JOIN marketplace_order_lines mol
                      ON mol.id = ia.marketplace_order_line_id
                    JOIN marketplace_orders mo
                      ON mo.id = mol.marketplace_order_id
                    WHERE mo.status = 'cancelled'
                      AND ib.batch_type = 'purchase'
                )
                """
            )
        )
    return released_total


def upgrade() -> None:
    _restore_cancelled_order_allocations(op.get_bind())


def downgrade() -> None:
    # A restored unit may already have been sold or allocated to another order.
    # Re-consuming it automatically would corrupt FIFO, so this data repair is
    # intentionally irreversible.
    pass
