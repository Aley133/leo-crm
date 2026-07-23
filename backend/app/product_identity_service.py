from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .inventory_service import allocate_order_line_fifo
from .models import MarketplaceOrderLine
from .order_line_product_linking import find_product_for_order_line
from .product_identity_models import (
    MarketplaceListing,
    MarketplaceListingIdentityKind,
    MarketplaceListingIssue,
    MarketplaceListingIssueReason,
    MarketplaceListingIssueStatus,
    MarketplaceListingStatus,
)


@dataclass(frozen=True, slots=True)
class ListingIdentity:
    kind: str
    raw_value: str
    identity_key: str


@dataclass(frozen=True, slots=True)
class ListingEnsureResult:
    listing_id: int | None
    issue_id: int | None
    missing_identity: bool


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def select_listing_identity(
    *, merchant_sku: str | None, external_product_id: str | None
) -> ListingIdentity | None:
    cleaned_sku = _clean(merchant_sku)
    if cleaned_sku is not None:
        kind = MarketplaceListingIdentityKind.MERCHANT_SKU.value
        return ListingIdentity(
            kind=kind,
            raw_value=cleaned_sku,
            identity_key=f"{kind}:{cleaned_sku}",
        )

    cleaned_external_id = _clean(external_product_id)
    if cleaned_external_id is not None:
        kind = MarketplaceListingIdentityKind.EXTERNAL_PRODUCT_ID.value
        return ListingIdentity(
            kind=kind,
            raw_value=cleaned_external_id,
            identity_key=f"{kind}:{cleaned_external_id}",
        )

    return None


def _dialect_insert(session: Session, model):
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return postgresql_insert(model)
    if dialect_name == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(
        "Marketplace listing upsert requires a dialect with explicit ON CONFLICT support"
    )


def _ensure_missing_identity_issue(
    session: Session,
    *,
    order_line: MarketplaceOrderLine,
) -> MarketplaceListingIssue:
    statement = _dialect_insert(session, MarketplaceListingIssue).values(
        marketplace_order_line_id=order_line.id,
        reason=MarketplaceListingIssueReason.MISSING_IDENTITY.value,
        status=MarketplaceListingIssueStatus.OPEN.value,
        title_snapshot=order_line.title,
        details="Both merchant_sku and external_product_id are absent or blank",
    )
    statement = statement.on_conflict_do_update(
        index_elements=[MarketplaceListingIssue.marketplace_order_line_id],
        set_={
            "reason": MarketplaceListingIssueReason.MISSING_IDENTITY.value,
            "status": MarketplaceListingIssueStatus.OPEN.value,
            "title_snapshot": order_line.title,
            "details": "Both merchant_sku and external_product_id are absent or blank",
            "resolved_at": None,
            "updated_at": datetime.now(UTC),
        },
    )
    session.execute(statement)
    return session.scalar(
        select(MarketplaceListingIssue).where(
            MarketplaceListingIssue.marketplace_order_line_id == order_line.id
        )
    )


def _resolve_open_issue(session: Session, *, order_line_id: int) -> None:
    issue = session.scalar(
        select(MarketplaceListingIssue).where(
            MarketplaceListingIssue.marketplace_order_line_id == order_line_id,
            MarketplaceListingIssue.status == MarketplaceListingIssueStatus.OPEN.value,
        )
    )
    if issue is not None:
        issue.status = MarketplaceListingIssueStatus.RESOLVED.value
        issue.resolved_at = datetime.now(UTC)


def ensure_marketplace_listing_for_order_line(
    session: Session,
    *,
    marketplace_account_id: int,
    order_line: MarketplaceOrderLine,
) -> ListingEnsureResult:
    """Ensure listing identity, catalogue resolution and FIFO stock allocation.

    Merchant SKU is shared by Kaspi order entries and the seller XML. When the
    matching Product already exists, listing resolution and inventory allocation
    happen in the same caller-owned import transaction. Repeating the same import
    is safe because FIFO allocations are idempotent per order line and batch.
    """

    if order_line.id is None:
        session.flush()

    identity = select_listing_identity(
        merchant_sku=order_line.merchant_sku,
        external_product_id=order_line.external_product_id,
    )
    if identity is None:
        issue = _ensure_missing_identity_issue(session, order_line=order_line)
        return ListingEnsureResult(
            listing_id=None,
            issue_id=issue.id,
            missing_identity=True,
        )

    values = {
        "marketplace_account_id": marketplace_account_id,
        "identity_kind": identity.kind,
        "identity_key": identity.identity_key,
        "merchant_sku": _clean(order_line.merchant_sku),
        "external_product_id": _clean(order_line.external_product_id),
        "product_id": None,
        "status": MarketplaceListingStatus.UNRESOLVED.value,
    }
    statement = _dialect_insert(session, MarketplaceListing).values(**values)
    statement = statement.on_conflict_do_nothing(
        index_elements=[
            MarketplaceListing.marketplace_account_id,
            MarketplaceListing.identity_key,
        ]
    )
    session.execute(statement)

    listing = session.scalar(
        select(MarketplaceListing).where(
            MarketplaceListing.marketplace_account_id == marketplace_account_id,
            MarketplaceListing.identity_key == identity.identity_key,
        )
    )
    if listing is None:
        raise RuntimeError("Marketplace listing upsert completed without a readable row")

    if listing.merchant_sku is None:
        listing.merchant_sku = _clean(order_line.merchant_sku)
    if listing.external_product_id is None:
        listing.external_product_id = _clean(order_line.external_product_id)

    product = find_product_for_order_line(session, order_line)
    if product is not None:
        order_line.product_id = product.id
        listing.product_id = product.id
        listing.status = MarketplaceListingStatus.RESOLVED.value
        listing.resolved_at = listing.resolved_at or datetime.now(UTC)
        if not order_line.title or order_line.title.strip().casefold() in {
            "unknown product",
            "название не получено",
        }:
            order_line.title = product.name
        session.flush()
        allocate_order_line_fifo(session, order_line=order_line)

    _resolve_open_issue(session, order_line_id=order_line.id)
    return ListingEnsureResult(
        listing_id=listing.id,
        issue_id=None,
        missing_identity=False,
    )
