from __future__ import annotations

from collections.abc import Iterable
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
    if conditions:
        exact = session.scalar(
            select(Product).where(or_(*conditions)).order_by(Product.id).limit(1)
        )
        if exact is not None:
            return exact

    # A fresh Kaspi order often exposes only the numeric master-product ID,
    # while Seller XML identifies the same offer as ``<master_id>_<offer_id>``.
    # Resolve that representation immediately when it is unambiguous instead
    # of leaving the line as ``Unknown product`` until Kaspi's slower product
    # endpoint happens to return the merchant SKU.
    if external_product_id is None or not external_product_id.isdigit():
        return None
    compound_prefix = f"{external_product_id}_%"
    candidates = list(
        session.scalars(
            select(Product)
            .where(
                or_(
                    Product.merchant_sku.like(compound_prefix),
                    Product.kaspi_product_id.like(compound_prefix),
                )
            )
            .order_by(Product.id)
            .limit(2)
        ).all()
    )
    return candidates[0] if len(candidates) == 1 else None


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


def _product_identity_map(products: Iterable[Product]) -> dict[str, Product]:
    """Build one deterministic identity map for a bulk XML import.

    The XML merchant SKU is authoritative. ``setdefault`` keeps the first stored
    product when malformed source data contains a duplicate identity, matching the
    previous ``ORDER BY Product.id`` behaviour.
    """

    identity_map: dict[str, Product] = {}
    for product in products:
        for identity in (_clean(product.merchant_sku), _clean(product.kaspi_product_id)):
            if identity is not None:
                identity_map.setdefault(identity, product)
    return identity_map


def link_all_matching_order_lines_for_products(
    session: Session,
    *,
    products: Iterable[Product],
) -> int:
    """Link all matching order lines in one database read.

    The old XML commit called ``link_all_matching_order_lines`` once per product.
    A normal 1,600-item catalog therefore produced thousands of SQL statements and
    could exceed Render's HTTP timeout. This implementation builds an in-memory
    identity map and scans the existing order lines once.
    """

    identity_map = _product_identity_map(products)
    if not identity_map:
        return 0

    linked = 0
    lines = session.scalars(
        select(MarketplaceOrderLine).where(
            or_(
                MarketplaceOrderLine.merchant_sku.is_not(None),
                MarketplaceOrderLine.external_product_id.is_not(None),
            )
        )
    )
    for line in lines:
        product = None
        for identity in (_clean(line.merchant_sku), _clean(line.external_product_id)):
            if identity is not None:
                product = identity_map.get(identity)
                if product is not None:
                    break
        if product is None:
            continue

        line.product_id = product.id
        if not line.title or line.title.strip().casefold() in {"unknown product", "название не получено"}:
            line.title = product.name
        linked += 1
    return linked


def link_all_matching_order_lines(session: Session, *, product: Product) -> int:
    """Backward-compatible single-product wrapper."""

    return link_all_matching_order_lines_for_products(session, products=(product,))
