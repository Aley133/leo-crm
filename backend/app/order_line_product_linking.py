from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import MarketplaceOrderLine, Product
from .product_identity_models import MarketplaceListing, MarketplaceListingStatus


_ORDER_LINE_LOOKUP_BATCH_SIZE = 400


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

    # Once stock has moved, automatic identity enrichment must not move the
    # order to a different warehouse card. Explicit reassignment is separate.
    if line.product_id is not None:
        linked = session.get(Product, line.product_id)
        if linked is not None:
            return linked

    sku = _clean(line.merchant_sku)
    external_product_id = _clean(line.external_product_id)
    for identity in (sku, external_product_id):
        if identity is None:
            continue
        for field in (Product.merchant_sku, Product.kaspi_product_id):
            exact = session.scalar(
                select(Product).where(field == identity).order_by(Product.id).limit(1)
            )
            if exact is not None:
                return exact

    # Accept only the numeric master_offer representation, in both directions.
    # An arbitrary SKU containing underscores is never a catalogue identity.
    for identity in (sku, external_product_id):
        if identity is None:
            continue
        base = compound_master_id(identity)
        if base is not None:
            candidates = list(session.scalars(select(Product).where(or_(
                Product.merchant_sku == base, Product.kaspi_product_id == base,
            )).limit(2)).all())
        elif identity.isdigit():
            candidates = list(session.scalars(select(Product).where(or_(
                Product.merchant_sku.startswith(identity + "_", autoescape=True),
                Product.kaspi_product_id.startswith(identity + "_", autoescape=True),
            )).order_by(Product.id).limit(2)).all())
            if len(candidates) == 1 and identity not in {
                compound_master_id(candidates[0].merchant_sku),
                compound_master_id(candidates[0].kaspi_product_id),
            }:
                candidates = []
        else:
            continue
        if len(candidates) == 1:
            return candidates[0]
    return None


def compound_master_id(identity: str | None) -> str | None:
    parts = (identity or "").split("_")
    return parts[0] if len(parts) == 2 and all(part.isdigit() for part in parts) else None


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
    """Link matching order lines with bounded, identity-indexed reads.

    The old XML commit called ``link_all_matching_order_lines`` once per product.
    A normal 1,600-item catalog therefore produced thousands of SQL statements and
    could exceed Render's HTTP timeout. A later implementation scanned the whole
    order-line history once, which grew slower over time. This implementation uses
    the imported identities and existing SKU indexes, so unrelated history is never
    materialized in the web process.
    """

    identity_map = _product_identity_map(products)
    if not identity_map:
        return 0

    linked = 0
    seen_line_ids: set[int] = set()
    identities = tuple(identity_map)
    for index in range(0, len(identities), _ORDER_LINE_LOOKUP_BATCH_SIZE):
        batch = identities[index : index + _ORDER_LINE_LOOKUP_BATCH_SIZE]
        lines = session.scalars(
            select(MarketplaceOrderLine)
            .where(
                or_(
                    MarketplaceOrderLine.merchant_sku.in_(batch),
                    MarketplaceOrderLine.external_product_id.in_(batch),
                )
            )
            .execution_options(yield_per=500)
        )
        for line in lines:
            if line.id in seen_line_ids:
                continue
            seen_line_ids.add(line.id)
            product = None
            for identity in (_clean(line.merchant_sku), _clean(line.external_product_id)):
                if identity is not None:
                    product = identity_map.get(identity)
                    if product is not None:
                        break
            if product is None:
                continue

            # XML imports must retain existing warehouse links.
            if line.product_id is not None and line.product_id != product.id:
                continue
            line.product_id = product.id
            if not line.title or line.title.strip().casefold() in {"unknown product", "название не получено"}:
                line.title = product.name
            linked += 1
    return linked


def link_all_matching_order_lines(session: Session, *, product: Product) -> int:
    """Backward-compatible single-product wrapper."""

    return link_all_matching_order_lines_for_products(session, products=(product,))
