from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .workspace_context import WorkspaceOwned


class ProductTestItem(WorkspaceOwned, Base):
    __tablename__ = "product_test_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "merchant_sku", name="uq_product_test_workspace_sku"),
        Index("ix_product_test_items_workspace_active_updated", "workspace_id", "active", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    input_reference: Mapped[str] = mapped_column(Text, nullable=False)
    kaspi_product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    merchant_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    kaspi_url: Mapped[str] = mapped_column(Text, nullable=False)
    supplier_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    test_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    preorder_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    city_id: Mapped[str] = mapped_column(String(32), nullable=False, default="196220100", server_default="196220100")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    offers_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(48), nullable=False, default="candidate", server_default="candidate", index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    fast_dumping_policy_id: Mapped[int | None] = mapped_column(ForeignKey("fast_dumping_policies.id", ondelete="SET NULL"), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ProductTestJob(WorkspaceOwned, Base):
    __tablename__ = "product_test_jobs"
    __table_args__ = (
        Index("ix_product_test_jobs_workspace_status_created", "workspace_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="inspect", server_default="inspect", index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("product_test_items.id", ondelete="CASCADE"), nullable=True, index=True)
    input_reference: Mapped[str] = mapped_column(Text, nullable=False)
    city_id: Mapped[str] = mapped_column(String(32), nullable=False, default="196220100", server_default="196220100")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", server_default="queued", index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    options_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductTestSettings(WorkspaceOwned, Base):
    __tablename__ = "product_test_settings"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_product_test_settings_workspace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_new: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    max_kaspi_scan: Mapped[int] = mapped_column(Integer, nullable=False, default=200, server_default="200")
    max_ozon_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    image_verify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    preorder_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    minimum_profit_kzt: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("1000"), server_default="1000")
    undercut_step_kzt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    allow_price_raise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    max_undercut_gap_percent: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False, default=Decimal("35"), server_default="35")
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600, server_default="600")
    delivery_price_premium_kzt: Mapped[int] = mapped_column(Integer, nullable=False, default=500, server_default="500")
    delivery_advantage_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    preorder_target_position: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    city_id: Mapped[str] = mapped_column(String(32), nullable=False, default="196220100", server_default="196220100")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
