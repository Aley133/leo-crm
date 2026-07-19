import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class BindingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    REJECTED = "rejected"


class MonitorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED = "degraded"
    MANUAL_REVIEW = "manual_review"
    DISABLED = "disabled"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"


class SourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    CAPTCHA_REQUIRED = "captcha_required"
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"
    DISABLED = "disabled"


class MonitorTarget(Base):
    __tablename__ = "monitor_targets"
    __table_args__ = (UniqueConstraint("product_binding_id", name="uq_monitor_target_binding"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_binding_id: Mapped[int] = mapped_column(
        ForeignKey("product_bindings.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=MonitorStatus.ACTIVE.value, server_default="active", index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    shard: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MonitorAttempt(Base):
    __tablename__ = "monitor_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_target_id: Mapped[int] = mapped_column(
        ForeignKey("monitor_targets.id", ondelete="CASCADE"), index=True
    )
    lease_token: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    adapter_code: Mapped[str] = mapped_column(String(64))
    access_strategy: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierOfferState(Base):
    __tablename__ = "supplier_offer_states"
    __table_args__ = (UniqueConstraint("supplier_product_id", name="uq_supplier_offer_state_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_product_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_products.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    adapter_schema_version: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SupplierOfferObservation(Base):
    __tablename__ = "supplier_offer_observations"
    __table_args__ = (
        UniqueConstraint("monitor_attempt_id", name="uq_supplier_observation_attempt"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_product_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_products.id", ondelete="CASCADE"), index=True
    )
    monitor_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitor_attempts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    adapter_schema_version: Mapped[str] = mapped_column(String(32))
    raw_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceHealth(Base):
    __tablename__ = "source_health"
    __table_args__ = (
        UniqueConstraint("supplier_id", "access_strategy", name="uq_source_health_supplier_strategy"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    access_strategy: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default=SourceHealthStatus.HEALTHY.value, server_default="healthy", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def offer_fingerprint(
    *,
    supplier_product_id: int,
    price: Decimal | None,
    available: bool | None,
    stock: int | None,
    delivery_days: int | None,
    seller: str | None,
    adapter_schema_version: str,
) -> str:
    """Return a stable SHA-256 fingerprint from normalized business facts."""
    payload = {
        "supplier_product_id": supplier_product_id,
        "price": format(price, "f") if price is not None else None,
        "available": available,
        "stock": stock,
        "delivery_days": delivery_days,
        "seller": " ".join((seller or "").split()).casefold() or None,
        "adapter_schema_version": adapter_schema_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
