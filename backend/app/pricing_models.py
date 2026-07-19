from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class PriceCalculationStatus(StrEnum):
    READY = "ready"
    POLICY_DISABLED = "policy_disabled"
    OFFER_MISSING = "offer_missing"
    OFFER_UNAVAILABLE = "offer_unavailable"
    CURRENCY_MISSING = "currency_missing"
    FX_MISSING = "fx_missing"
    INVALID_POLICY = "invalid_policy"


class PricingPolicy(Base):
    __tablename__ = "pricing_policies"
    __table_args__ = (UniqueConstraint("product_id", name="uq_pricing_policy_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    target_margin_pct: Mapped[float] = mapped_column(Numeric(7, 4), default=30, server_default="30")
    marketplace_fee_pct: Mapped[float] = mapped_column(Numeric(7, 4), default=12, server_default="12")
    payment_fee_pct: Mapped[float] = mapped_column(Numeric(7, 4), default=3, server_default="3")
    delivery_cost_kzt: Mapped[float] = mapped_column(Numeric(14, 2), default=0, server_default="0")
    fixed_cost_kzt: Mapped[float] = mapped_column(Numeric(14, 2), default=0, server_default="0")
    minimum_price_kzt: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    rounding_step_kzt: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FxRateSnapshot(Base):
    __tablename__ = "fx_rate_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), index=True)
    quote_currency: Mapped[str] = mapped_column(String(3), index=True)
    rate: Mapped[float] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PriceCalculation(Base):
    __tablename__ = "price_calculations"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    pricing_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("pricing_policies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_offer_state_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_offer_states.id", ondelete="SET NULL"), nullable=True, index=True
    )
    fx_rate_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("fx_rate_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    supplier_price: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    supplier_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    fx_rate_to_kzt: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    supplier_cost_kzt: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    delivery_cost_kzt: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    fixed_cost_kzt: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_fee_pct: Mapped[float | None] = mapped_column(Numeric(7, 4), nullable=True)
    target_margin_pct: Mapped[float | None] = mapped_column(Numeric(7, 4), nullable=True)
    recommended_price_kzt: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    explanation_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
