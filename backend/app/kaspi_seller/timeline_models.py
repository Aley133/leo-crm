from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


class KaspiSellerOrderTimelineEvent(Base):
    """Immutable business event derived from normalized Seller snapshots."""

    __tablename__ = "kaspi_seller_order_timeline_events"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "event_type",
            name="uq_kaspi_seller_timeline_snapshot_event",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("kaspi_seller_order_snapshots.id", ondelete="CASCADE"),
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
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_payload: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
