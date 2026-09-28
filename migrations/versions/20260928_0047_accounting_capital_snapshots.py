"""Add append-only accounting capital snapshots.

Revision ID: 20260928_0047
Revises: 20260906_0046
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_0047"
down_revision: str | None = "20260906_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounting_capital_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("cash_balance_kzt", sa.Numeric(18, 2), nullable=False),
        sa.Column("free_capital_kzt", sa.Numeric(18, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cash_balance_kzt >= 0",
            name="ck_accounting_capital_cash_nonnegative",
        ),
        sa.CheckConstraint(
            "free_capital_kzt >= 0",
            name="ck_accounting_capital_free_nonnegative",
        ),
        sa.CheckConstraint(
            "free_capital_kzt <= cash_balance_kzt",
            name="ck_accounting_capital_free_within_cash",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_accounting_capital_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_accounting_capital_snapshots_workspace_id",
        "accounting_capital_snapshots",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_accounting_capital_workspace_created",
        "accounting_capital_snapshots",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_accounting_capital_workspace_created",
        table_name="accounting_capital_snapshots",
    )
    op.drop_index(
        "ix_accounting_capital_snapshots_workspace_id",
        table_name="accounting_capital_snapshots",
    )
    op.drop_table("accounting_capital_snapshots")
