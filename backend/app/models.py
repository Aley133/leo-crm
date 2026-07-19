from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
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


class ProductStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class MarketplaceProvider(StrEnum):
    KASPI = "kaspi"


class MarketplaceOrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    ASSEMBLY = "assembly"
    SHIPPING = "shipping"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"
    UNKNOWN = "unknown"


class MarketplaceImportStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    kaspi_product_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    merchant_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=ProductStatus.DRAFT.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class MarketplaceAccount(Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_account_id",
            name="uq_marketplace_account_provider_external",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    external_account_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    orders: Mapped[list["MarketplaceOrder"]] = relationship(back_populates="account")


class MarketplaceImportExecution(Base):
    __tablename__ = "marketplace_import_executions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=MarketplaceImportStatus.RUNNING.value, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketplaceImportCheckpoint(Base):
    __tablename__ = "marketplace_import_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_account_id",
            "stream_name",
            name="uq_marketplace_checkpoint_account_stream",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True
    )
    stream_name: Mapped[str] = mapped_column(String(64))
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketplaceOrder(Base):
    __tablename__ = "marketplace_orders"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_account_id",
            "external_order_id",
            name="uq_marketplace_order_account_external",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="RESTRICT"), index=True
    )
    external_order_id: Mapped[str] = mapped_column(String(128))
    external_code: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=MarketplaceOrderStatus.UNKNOWN.value, index=True
    )
    original_status: Mapped[str] = mapped_column(String(128), default="unknown", index=True)
    source_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    total_amount: Mapped[float] = mapped_column(Numeric(18, 2))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    planned_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    account: Mapped[MarketplaceAccount] = relationship(back_populates="orders")
    lines: Mapped[list["MarketplaceOrderLine"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    events: Mapped[list["MarketplaceOrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class MarketplaceOrderLine(Base):
    __tablename__ = "marketplace_order_lines"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_order_id",
            "external_line_id",
            name="uq_marketplace_order_line_external",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_order_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_orders.id", ondelete="CASCADE"), index=True
    )
    external_line_id: Mapped[str] = mapped_column(String(128))
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True, nullable=True
    )
    external_product_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    merchant_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Numeric(18, 2))
    line_total: Mapped[float] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[MarketplaceOrder] = relationship(back_populates="lines")


class MarketplaceOrderEvent(Base):
    __tablename__ = "marketplace_order_events"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_order_id",
            "source_event_key",
            name="uq_marketplace_order_event_source_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_order_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_orders.id", ondelete="CASCADE"), index=True
    )
    source_event_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[MarketplaceOrder] = relationship(back_populates="events")


class MarketplaceRawPayload(Base):
    __tablename__ = "marketplace_raw_payloads"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_account_id",
            "payload_type",
            "external_object_id",
            "content_hash",
            name="uq_marketplace_raw_payload_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"), index=True
    )
    import_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("marketplace_import_executions.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    payload_type: Mapped[str] = mapped_column(String(64), index=True)
    external_object_id: Mapped[str] = mapped_column(String(128), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
