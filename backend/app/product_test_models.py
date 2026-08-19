from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
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
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ProductTestJob(WorkspaceOwned, Base):
    __tablename__ = "product_test_jobs"
    __table_args__ = (
        Index("ix_product_test_jobs_workspace_status_created", "workspace_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    input_reference: Mapped[str] = mapped_column(Text, nullable=False)
    city_id: Mapped[str] = mapped_column(String(32), nullable=False, default="196220100", server_default="196220100")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", server_default="queued", index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
