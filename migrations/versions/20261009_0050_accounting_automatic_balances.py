"""Freeze operation totals when calibrating accounting balances."""
from alembic import op
import sqlalchemy as sa

revision: str = "20261009_0050"
down_revision: str | None = "20261006_0049"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("sales_receipts_kzt", "sales_profit_kzt", "purchases_kzt"):
        op.add_column("accounting_capital_snapshots", sa.Column(name, sa.Numeric(18, 2), nullable=True))


def downgrade():
    for name in ("purchases_kzt", "sales_profit_kzt", "sales_receipts_kzt"):
        op.drop_column("accounting_capital_snapshots", name)
