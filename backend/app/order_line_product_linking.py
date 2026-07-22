from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import MarketplaceOrderLine, Product
from .product_identity_models import MarketplaceListing, MarketplaceListingStatus


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def find_product_for_order_line(session: Session, line: MarketplaceOrderLine) -> Product | None:
    """Resolve an order line against the product registry using stable identities.

    Merchant SKU is authoritative because it is shared by Kaspi order entries and
    the seller XML. Kaspi product ID is a safe secondary identity.
    """

    sku = _clean(line.merchant_sku)
    external_product_id = _clean(line.external_product_id)
    conditions = []
    if sku is not None:
        conditions.append(Product.merchant_sku == sku)
        conditions.append(Product.kaspi_product_id == sku)
    if external_product_id is not None:
        conditions.append(Product.kaspi_product_id == external_product_id)
        conditions.append(Product.merchant_sku == external_product_id)
    if not conditions:
        return None
    return session.scalar(select(Product).where(or_(*conditions)).order_by(Product.id).limit(1))


def link_order_line_to_product(
    session: Session,
    *,
    marketplace_account_id: int,
    line: MarketplaceOrderLine,
) -> Product | None:
    product = find_product_for_order_line(session, line)
    if product is None:
        return None

    line.product_id = product.id
    if not line.title or line.title.strip().casefold() in {"unknown product", "название не получено"}:
        line.title = product.name

    identities = []
    if _clean(line.merchant_sku):
        identities.append(f"merchant_sku:{line.merchant_sku.strip()}")
    if _clean(line.external_product_id):
        identities.append(f"external_product_id:{line.external_product_id.strip()}")

    for identity_key in identities:
        listing = session.scalar(
            select(MarketplaceListing).where(
                MarketplaceListing.marketplace_account_id == marketplace_account_id,
                MarketplaceListing.identity_key == identity_key,
            )
        )
        if listing is not None:
            listing.product_id = product.id
            listing.status = MarketplaceListingStatus.RESOLVED.value
            listing.resolved_at = listing.resolved_at or datetime.now(UTC)

    return product


def link_all_matching_order_lines(session: Session, *, product: Product) -> int:
    conditions = []
    if _clean(product.merchant_sku):
        conditions.append(MarketplaceOrderLine.merchant_sku == product.merchant_sku.strip())
        conditions.append(MarketplaceOrderLine.external_product_id == product.merchant_sku.strip())
    if _clean(product.kaspi_product_id):
        conditions.append(MarketplaceOrderLine.external_product_id == product.kaspi_product_id.strip())
        conditions.append(MarketplaceOrderLine.merchant_sku == product.kaspi_product_id.strip())
    if not conditions:
        return 0

    linked = 0
    for line in session.scalars(select(MarketplaceOrderLine).where(or_(*conditions))):
        line.product_id = product.id
        if not line.title or line.title.strip().casefold() in {"unknown product", "название не получено"}:
            line.title = product.name
        linked += 1
    return linked
