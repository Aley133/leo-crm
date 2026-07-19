"""Scope source health by supplier and access strategy.

Revision ID: 20260719_0006
Revises: 20260719_0005
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0006"
down_revision: str | None = "20260719_0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "source_health",
        sa.Column("access_strategy", sa.String(length=64), nullable=True),
    )
    op.execute("UPDATE source_health SET access_strategy = 'direct_http' WHERE access_strategy IS NULL")
    op.alter_column("source_health", "access_strategy", nullable=False)
    op.drop_constraint("uq_source_health_supplier", "source_health", type_="unique")
    op.create_index(
        "ix_source_health_access_strategy",
        "source_health",
        ["access_strategy"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_source_health_supplier_strategy",
        "source_health",
        ["supplier_id", "access_strategy"],
    )


def downgrade() -> None:
    # Downgrade is only safe when there is at most one strategy row per supplier.
    op.drop_constraint(
        "uq_source_health_supplier_strategy",
        "source_health",
        type_="unique",
    )
    op.drop_index("ix_source_health_access_strategy", table_name="source_health")
    op.create_unique_constraint(
        "uq_source_health_supplier",
        "source_health",
        ["supplier_id"],
    )
    op.drop_column("source_health", "access_strategy")
