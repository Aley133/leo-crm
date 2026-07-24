from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class DailyRevenueSnapshot(Base):
    __tablename__ = "daily_revenue_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_account_id",
            "business_date",
            name="uq_daily_revenue_snapshot_account_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    business_date: Mapped[date] = mapped_column(Date(), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    source_stage: Mapped[str] = mapped_column(String(32), default="assembly")
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    units_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    net_profit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    margin_pct: Mapped[Decimal] = mapped_column(Numeric(9, 4), default=0)
    order_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
