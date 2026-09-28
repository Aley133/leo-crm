from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .workspace_context import WorkspaceOwned


class AccountingCapitalSnapshot(WorkspaceOwned, Base):
    """Append-only owner-entered liquid-capital position for one workspace."""

    __tablename__ = "accounting_capital_snapshots"
    __table_args__ = (
        CheckConstraint(
            "cash_balance_kzt >= 0",
            name="ck_accounting_capital_cash_nonnegative",
        ),
        CheckConstraint(
            "free_capital_kzt >= 0",
            name="ck_accounting_capital_free_nonnegative",
        ),
        CheckConstraint(
            "free_capital_kzt <= cash_balance_kzt",
            name="ck_accounting_capital_free_within_cash",
        ),
        Index(
            "ix_accounting_capital_workspace_created",
            "workspace_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cash_balance_kzt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    free_capital_kzt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
