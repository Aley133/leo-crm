from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class MarketplaceListingStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class MarketplaceListingIdentityKind(StrEnum):
    MERCHANT_SKU = "merchant_sku"
    EXTERNAL_PRODUCT_ID = "external_product_id"


class MarketplaceListingIssueReason(StrEnum):
    MISSING_IDENTITY = "missing_identity"


class MarketplaceListingIssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class MarketplaceListing(Base):
    __tablename__ = "marketplace_listings"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_account_id",
            "identity_key",
            name="uq_marketplace_listing_account_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    identity_kind: Mapped[str] = mapped_column(String(32), index=True)
    identity_key: Mapped[str] = mapped_column(String(300))
    merchant_sku: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    external_product_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=MarketplaceListingStatus.UNRESOLVED.value,
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketplaceListingIssue(Base):
    __tablename__ = "marketplace_listing_issues"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_order_line_id",
            name="uq_marketplace_listing_issue_order_line",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace_order_line_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_order_lines.id", ondelete="CASCADE"),
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=MarketplaceListingIssueStatus.OPEN.value,
        index=True,
    )
    title_snapshot: Mapped[str] = mapped_column(String(500))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
