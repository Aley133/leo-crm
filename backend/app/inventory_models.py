from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .db_types import UTCDateTime


class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    __table_args__ = (
        CheckConstraint("quantity_received > 0", name="ck_inventory_batch_received_positive"),
        CheckConstraint(
            "quantity_remaining >= 0 AND quantity_remaining <= quantity_received",
            name="ck_inventory_batch_remaining_range",
        ),
        CheckConstraint("unit_cost >= 0", name="ck_inventory_batch_unit_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    quantity_received: Mapped[int] = mapped_column(Integer)
    quantity_remaining: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    allocations: Mapped[list["InventoryAllocation"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class InventoryAllocation(Base):
    __tablename__ = "inventory_allocations"
    __table_args__ = (
        UniqueConstraint(
            "inventory_batch_id",
            "marketplace_order_line_id",
            name="uq_inventory_allocation_batch_order_line",
        ),
        CheckConstraint("quantity > 0", name="ck_inventory_allocation_quantity_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_inventory_allocation_unit_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inventory_batch_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("inventory_batches.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    marketplace_order_line_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("marketplace_order_lines.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    allocated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    batch: Mapped[InventoryBatch] = relationship(back_populates="allocations")