"""Freeze operation totals when calibrating accounting balances."""
from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime

revision: str = "20261009_0050"
down_revision: str | None = "20261006_0049"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("sales_receipts_kzt", "sales_profit_kzt", "purchases_kzt"):
        op.add_column("accounting_capital_snapshots", sa.Column(name, sa.Numeric(18, 2), nullable=True))
    _restore_completion_dates()


def _restore_completion_dates():
    connection = op.get_bind()
    orders = sa.table("marketplace_orders", sa.column("id", sa.Integer),
        sa.column("workspace_id", sa.Integer), sa.column("marketplace_account_id", sa.Integer),
        sa.column("external_order_id", sa.String), sa.column("status", sa.String),
        sa.column("delivered_at", sa.DateTime(timezone=True)))
    raw = sa.table("marketplace_raw_payloads", sa.column("id", sa.Integer),
        sa.column("workspace_id", sa.Integer), sa.column("marketplace_account_id", sa.Integer),
        sa.column("external_object_id", sa.String), sa.column("payload_type", sa.String),
        sa.column("received_at", sa.DateTime(timezone=True)), sa.column("payload_json", sa.JSON))
    completion = raw.c.payload_json["attributes"]["completionDate"].as_string()
    rows = connection.execute(sa.select(orders.c.id, completion).select_from(
        orders.join(raw, sa.and_(orders.c.workspace_id == raw.c.workspace_id,
            orders.c.marketplace_account_id == raw.c.marketplace_account_id,
            orders.c.external_order_id == raw.c.external_object_id))
    ).where(orders.c.delivered_at.is_(None), orders.c.status.in_(["delivered", "returned"]),
        raw.c.payload_type == "order", completion.is_not(None)
    ).order_by(raw.c.received_at.desc(), raw.c.id.desc()))
    recovered = {}
    for order_id, value in rows:
        if order_id in recovered:
            continue
        try:
            milliseconds = int(value)
            if not 0 < milliseconds < 4102444800000:
                continue
            recovered[order_id] = datetime.fromtimestamp(milliseconds / 1000, UTC)
        except (ValueError, TypeError, OverflowError):
            continue
    if recovered:
        connection.execute(sa.update(orders).where(orders.c.id == sa.bindparam("order_id"),
            orders.c.delivered_at.is_(None)).values(delivered_at=sa.bindparam("issued_at")),
            [{"order_id": key, "issued_at": value} for key, value in recovered.items()])


def downgrade():
    for name in ("purchases_kzt", "sales_profit_kzt", "sales_receipts_kzt"):
        op.drop_column("accounting_capital_snapshots", name)
