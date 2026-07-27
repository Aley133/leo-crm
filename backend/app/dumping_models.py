from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class DumpingPolicy(Base):
    __tablename__ = "dumping_policies"
    __table_args__ = (UniqueConstraint("product_id", name="uq_dumping_policy_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    minimum_profit_kzt: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("1000"), server_default="1000"
    )
    undercut_step_kzt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    supplier_delivery_buffer_days: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    inventory_first: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    auto_publish_xml: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    city_id: Mapped[str] = mapped_column(String(32), default="750000000", server_default="750000000")
    zone_id: Mapped[str] = mapped_column(String(64), default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DumpingRun(Base):
    __tablename__ = "dumping_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    dumping_policy_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("dumping_policies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_cost_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    source_delivery_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    safe_floor_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    own_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    competitor_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    target_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    preorder_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    explanation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class KaspiXmlFeed(Base):
    __tablename__ = "kaspi_xml_feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        default=1,
        server_default="1",
    )
    merchant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_xml: Mapped[str] = mapped_column(Text, nullable=False)
    generated_xml: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
