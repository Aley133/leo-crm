from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .workspace_context import WorkspaceOwned


class FastDumpingPolicy(WorkspaceOwned, Base):
    """Per-product rules for the isolated realtime repricing channel."""

    __tablename__ = "fast_dumping_policies"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "product_id",
            name="uq_fast_dumping_policy_workspace_product",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    minimum_profit_kzt: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
        default=Decimal("1000"),
        server_default="1000",
    )
    undercut_step_kzt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    allow_price_raise: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    max_undercut_gap_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 2),
        nullable=False,
        default=Decimal("35"),
        server_default="35",
    )
    scan_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=600,
        server_default="600",
    )
    city_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="750000000",
        server_default="750000000",
    )
    zone_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="Magnum_ZONE1",
        server_default="Magnum_ZONE1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FastDumpingState(WorkspaceOwned, Base):
    """Latest durable decision and safety latch for one realtime product."""

    __tablename__ = "fast_dumping_states"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "product_id",
            name="uq_fast_dumping_state_workspace_product",
        ),
        Index(
            "ix_fast_dumping_states_workspace_status_id",
            "workspace_id",
            "status",
            "id",
        ),
        Index(
            "ix_fast_dumping_states_workspace_due_id",
            "workspace_id",
            "next_scan_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("fast_dumping_policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="idle",
        server_default="idle",
        index=True,
    )
    decision_status: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_cost_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    inventory_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    safe_floor_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    own_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    competitor_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    competitor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    desired_stock_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    own_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_model: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_visible_price_kzt: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    market_context_ok: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    market_context_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    offers_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    offers_json: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        deferred=True,
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    active_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    automatic_writes_paused: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_operation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FastDumpingJob(WorkspaceOwned, Base):
    """One scan/apply/verification lifecycle executed by the local fast agent."""

    __tablename__ = "fast_dumping_jobs"
    __table_args__ = (
        Index(
            "ix_fast_dumping_jobs_workspace_status_id",
            "workspace_id",
            "status",
            "id",
        ),
        Index(
            "ix_fast_dumping_jobs_product_status_id",
            "product_id",
            "status",
            "id",
        ),
        Index(
            "ix_fast_dumping_jobs_workspace_status_due_id",
            "workspace_id",
            "status",
            "not_before_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("fast_dumping_policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    not_before_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    scan_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    apply_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    market_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    decision_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    write_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
