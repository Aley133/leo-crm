from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class KaspiSellerOrderSnapshotRecord(Base):
    """Immutable observation of one normalized Kaspi Seller order snapshot."""

    __tablename__ = "kaspi_seller_order_snapshots"
    __table_args__ = (
        UniqueConstraint("browser_agent_job_id", name="uq_kaspi_seller_snapshots_browser_job"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    browser_agent_job_id: Mapped[int] = mapped_column(
        ForeignKey("browser_agent_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("kaspi_seller_order_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    merchant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    order_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    snapshot_payload: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
