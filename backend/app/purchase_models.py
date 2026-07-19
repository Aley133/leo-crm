from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .db_types import UTCDateTime


class PurchaseOrigin(StrEnum):
    MARKETPLACE_ORDER = "marketplace_order"
    MANUAL = "manual"


class PurchaseStatus(StrEnum):
    DRAFT = "draft"
    REQUESTED = "requested"
    ORDERED = "ordered"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class PurchaseRequest(Base):
    __tablename__ = "purchase_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    marketplace_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_orders.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    origin: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=PurchaseStatus.DRAFT.value, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    expected_total: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lines: Mapped[list["PurchaseRequestLine"]] = relationship(
        back_populates="purchase_request", cascade="all, delete-orphan"
    )
    events: Mapped[list["PurchaseEvent"]] = relationship(
        back_populates="purchase_request", cascade="all, delete-orphan"
    )
    receipts: Mapped[list["PurchaseReceipt"]] = relationship(
        back_populates="purchase_request", cascade="all, delete-orphan"
    )


class PurchaseRequestLine(Base):
    __tablename__ = "purchase_request_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_request_line_quantity_positive"),
        CheckConstraint(
            "received_quantity >= 0 AND received_quantity <= quantity",
            name="ck_purchase_request_line_received_quantity_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), index=True
    )
    marketplace_order_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("marketplace_order_lines.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)
    expected_unit_cost: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    purchase_request: Mapped[PurchaseRequest] = relationship(back_populates="lines")
    receipt_lines: Mapped[list["PurchaseReceiptLine"]] = relationship(
        back_populates="purchase_request_line"
    )


class PurchaseEvent(Base):
    __tablename__ = "purchase_events"
    __table_args__ = (
        UniqueConstraint(
            "purchase_request_id",
            "idempotency_key",
            name="uq_purchase_event_request_idempotency",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    purchase_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    purchase_request: Mapped[PurchaseRequest] = relationship(back_populates="events")


class PurchaseReceipt(Base):
    __tablename__ = "purchase_receipts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    purchase_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_requests.id", ondelete="CASCADE"), index=True
    )
    receipt_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    purchase_request: Mapped[PurchaseRequest] = relationship(back_populates="receipts")
    lines: Mapped[list["PurchaseReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )


class PurchaseReceiptLine(Base):
    __tablename__ = "purchase_receipt_lines"
    __table_args__ = (
        UniqueConstraint(
            "purchase_receipt_id",
            "purchase_request_line_id",
            name="uq_purchase_receipt_line_identity",
        ),
        CheckConstraint("quantity > 0", name="ck_purchase_receipt_line_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_receipt_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_receipts.id", ondelete="CASCADE"), index=True
    )
    purchase_request_line_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_request_lines.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)

    receipt: Mapped[PurchaseReceipt] = relationship(back_populates="lines")
    purchase_request_line: Mapped[PurchaseRequestLine] = relationship(
        back_populates="receipt_lines"
    )
