from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MarketplaceOrder, MarketplaceOrderLine, Product
from .product_identity_models import (
    MarketplaceListing,
    MarketplaceListingEvent,
    MarketplaceListingEventType,
    MarketplaceListingStatus,
)


def _matching_order_lines(session: Session, listing: MarketplaceListing) -> list[MarketplaceOrderLine]:
    query = (
        select(MarketplaceOrderLine)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .where(MarketplaceOrder.marketplace_account_id == listing.marketplace_account_id)
    )
    if listing.identity_kind == "merchant_sku":
        query = query.where(MarketplaceOrderLine.merchant_sku == listing.merchant_sku)
    else:
        query = query.where(
            MarketplaceOrderLine.external_product_id == listing.external_product_id
        )
    return list(session.scalars(query).all())


def resolve_listing(
    session: Session,
    *,
    listing_id: int,
    product_id: int,
    actor: str | None = None,
) -> MarketplaceListing:
    """Resolve or reassign a listing inside the caller-owned transaction."""
    listing = session.scalar(
        select(MarketplaceListing)
        .where(MarketplaceListing.id == listing_id)
        .with_for_update()
    )
    if listing is None:
        raise LookupError("Marketplace listing not found")
    if session.get(Product, product_id) is None:
        raise LookupError("Product not found")

    previous_product_id = listing.product_id
    if previous_product_id == product_id and listing.status == MarketplaceListingStatus.RESOLVED.value:
        return listing

    now = datetime.now(UTC)
    listing.product_id = product_id
    listing.status = MarketplaceListingStatus.RESOLVED.value
    listing.resolved_at = now
    for line in _matching_order_lines(session, listing):
        line.product_id = product_id

    session.add(
        MarketplaceListingEvent(
            marketplace_listing_id=listing.id,
            event_type=(
                MarketplaceListingEventType.RESOLVED.value
                if previous_product_id is None
                else MarketplaceListingEventType.REASSIGNED.value
            ),
            previous_product_id=previous_product_id,
            current_product_id=product_id,
            metadata_json={"actor": actor} if actor else None,
            occurred_at=now,
        )
    )
    session.flush()
    return listing


def unresolve_listing(
    session: Session,
    *,
    listing_id: int,
    actor: str | None = None,
) -> MarketplaceListing:
    """Unlink order lines while preserving historical purchase request lines."""
    listing = session.scalar(
        select(MarketplaceListing)
        .where(MarketplaceListing.id == listing_id)
        .with_for_update()
    )
    if listing is None:
        raise LookupError("Marketplace listing not found")

    previous_product_id = listing.product_id
    if previous_product_id is None and listing.status == MarketplaceListingStatus.UNRESOLVED.value:
        return listing

    now = datetime.now(UTC)
    listing.product_id = None
    listing.status = MarketplaceListingStatus.UNRESOLVED.value
    listing.resolved_at = None
    for line in _matching_order_lines(session, listing):
        line.product_id = None

    session.add(
        MarketplaceListingEvent(
            marketplace_listing_id=listing.id,
            event_type=MarketplaceListingEventType.UNRESOLVED.value,
            previous_product_id=previous_product_id,
            current_product_id=None,
            metadata_json={"actor": actor} if actor else None,
            occurred_at=now,
        )
    )
    session.flush()
    return listing
