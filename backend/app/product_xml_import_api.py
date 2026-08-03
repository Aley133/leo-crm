from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import unquote
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import DumpingPolicy, KaspiXmlFeed
from .dumping_service import (
    close_untracked_order_offer,
    sync_product_inventory_to_feed,
    workspace_feed_url,
)
from .inventory_models import InventoryBatch
from .kaspi_xml_import import KaspiXmlProduct, parse_kaspi_products
from .models import (
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    Product,
    ProductStatus,
)
from .order_line_product_linking import link_all_matching_order_lines_for_products
from .product_inventory_group import inventory_owner_ids_for_products


router = APIRouter(
    prefix="/api/product-registry/imports/xml",
    tags=["product-registry"],
    dependencies=[Depends(require_service_token)],
)

_LOOKUP_BATCH_SIZE = 400


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def _merchant_id(xml_bytes: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if _local_name(element.tag) == "merchantid":
            value = (element.text or "").strip()
            if value:
                return value[:128]
    return None


def _source_filename(request: Request) -> str | None:
    raw = request.headers.get("X-Filename")
    if not raw:
        return None
    value = unquote(raw).strip()
    return value[:255] or None


async def _read_products(request: Request) -> tuple[bytes, list[KaspiXmlProduct], list[str]]:
    body = await request.body()
    try:
        products, warnings = parse_kaspi_products(body)
        return body, products, warnings
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _sample(products: list[KaspiXmlProduct], *, limit: int = 10) -> list[dict]:
    return [
        {
            "kaspi_product_id": item.kaspi_product_id,
            "merchant_sku": item.merchant_sku,
            "name": item.name,
            "brand": item.brand,
        }
        for item in products[:limit]
    ]


def _chunks(values: list[str], size: int = _LOOKUP_BATCH_SIZE) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _existing_product_ids(db: Session, ids: list[str]) -> set[str]:
    result: set[str] = set()
    for batch in _chunks(ids):
        result.update(
            db.scalars(
                select(Product.kaspi_product_id).where(Product.kaspi_product_id.in_(batch))
            ).all()
        )
    return result


def _existing_products(db: Session, ids: list[str]) -> dict[str, Product]:
    result: dict[str, Product] = {}
    for batch in _chunks(ids):
        for product in db.scalars(
            select(Product).where(Product.kaspi_product_id.in_(batch))
        ).all():
            result[product.kaspi_product_id] = product
    return result


def _store_feed_source(
    db: Session,
    *,
    body: bytes,
    source_filename: str | None,
    activate: bool,
) -> KaspiXmlFeed:
    xml_text = body.decode("utf-8-sig")
    feed = db.scalar(select(KaspiXmlFeed).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None:
        feed = KaspiXmlFeed(
            merchant_id=_merchant_id(body),
            source_filename=source_filename,
            source_xml=xml_text,
            generated_xml=xml_text,
            active=activate,
        )
        db.add(feed)
    else:
        feed.merchant_id = _merchant_id(body) or feed.merchant_id
        feed.source_filename = source_filename or feed.source_filename
        feed.source_xml = xml_text
        if activate:
            feed.generated_xml = xml_text
            feed.active = True
    return feed


@router.post("/retain-source")
async def retain_xml_source(request: Request, db: Session = Depends(get_db)) -> dict:
    body, products, warnings = await _read_products(request)
    try:
        feed = _store_feed_source(
            db,
            body=body,
            source_filename=_source_filename(request),
            activate=False,
        )
        db.commit()
        db.refresh(feed)
    except Exception:
        db.rollback()
        raise
    return {
        "retained": True,
        "total": len(products),
        "warning_count": len(warnings),
        "source_filename": feed.source_filename,
        "merchant_id": feed.merchant_id,
    }


@router.post("/preview")
async def preview_xml_import(request: Request, db: Session = Depends(get_db)) -> dict:
    _body, products, warnings = await _read_products(request)
    ids = list(dict.fromkeys(item.kaspi_product_id for item in products))
    existing_ids = _existing_product_ids(db, ids)
    return {
        "total": len(products),
        "new_count": sum(1 for item in products if item.kaspi_product_id not in existing_ids),
        "existing_count": sum(1 for item in products if item.kaspi_product_id in existing_ids),
        "warning_count": len(warnings),
        "warnings": warnings,
        "sample": _sample(products),
    }


@router.post("/commit")
async def commit_xml_import(request: Request, db: Session = Depends(get_db)) -> dict:
    body, products, warnings = await _read_products(request)
    ids = list(dict.fromkeys(item.kaspi_product_id for item in products))
    existing = _existing_products(db, ids)

    created = 0
    updated = 0
    unchanged = 0
    linked_order_lines = 0
    try:
        stored_products: list[Product] = []
        for item in products:
            product = existing.get(item.kaspi_product_id)
            if product is None:
                product = Product(
                    kaspi_product_id=item.kaspi_product_id,
                    merchant_sku=item.merchant_sku,
                    name=item.name,
                    brand=item.brand,
                    status=ProductStatus.ACTIVE.value,
                )
                db.add(product)
                existing[item.kaspi_product_id] = product
                created += 1
            else:
                changed = False
                for field, value in (
                    ("merchant_sku", item.merchant_sku),
                    ("name", item.name),
                    ("brand", item.brand),
                ):
                    if value is not None and getattr(product, field) != value:
                        setattr(product, field, value)
                        changed = True
                if changed:
                    updated += 1
                else:
                    unchanged += 1
            stored_products.append(product)

        db.flush()
        linked_order_lines = link_all_matching_order_lines_for_products(
            db,
            products=stored_products,
        )

        db.execute(update(KaspiXmlFeed).values(active=False))
        feed = _store_feed_source(
            db,
            body=body,
            source_filename=_source_filename(request),
            activate=True,
        )
        feed.active = True
        managed_product_ids = set(
            db.scalars(
                select(DumpingPolicy.product_id).where(
                    DumpingPolicy.product_id.in_([product.id for product in stored_products])
                )
            ).all()
        )
        stored_product_ids = {int(product.id) for product in stored_products}
        owner_by_product = inventory_owner_ids_for_products(
            db,
            stored_product_ids,
        )
        inventory_owner_ids = set(
            int(value)
            for value in db.scalars(
                select(InventoryBatch.product_id)
                .where(
                    InventoryBatch.product_id.in_(set(owner_by_product.values()))
                )
                .distinct()
            ).all()
        )
        managed_product_ids.update(
            product_id
            for product_id, owner_id in owner_by_product.items()
            if owner_id in inventory_owner_ids
        )
        active_order_statuses = (
            MarketplaceOrderStatus.NEW.value,
            MarketplaceOrderStatus.ACCEPTED.value,
            MarketplaceOrderStatus.ASSEMBLY.value,
            MarketplaceOrderStatus.HANDOVER.value,
            MarketplaceOrderStatus.SHIPPING.value,
            MarketplaceOrderStatus.UNKNOWN.value,
            "preorder",
        )
        managed_product_ids.update(
            value
            for value in db.scalars(
                select(MarketplaceOrderLine.product_id)
                .join(
                    MarketplaceOrder,
                    MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id,
                )
                .where(
                    MarketplaceOrder.status.in_(active_order_statuses),
                    MarketplaceOrderLine.product_id.is_not(None),
                )
                .distinct()
            ).all()
            if value is not None
        )
        for product_id in sorted(int(value) for value in managed_product_ids):
            sync_product_inventory_to_feed(
                db,
                product_id=product_id,
                reason="xml_source_reimported",
            )
        unresolved_active_lines = db.execute(
            select(
                MarketplaceOrderLine.merchant_sku,
                MarketplaceOrderLine.external_product_id,
            )
            .join(
                MarketplaceOrder,
                MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id,
            )
            .where(
                MarketplaceOrder.status.in_(active_order_statuses),
                MarketplaceOrderLine.product_id.is_(None),
            )
        ).all()
        for merchant_sku, external_product_id in unresolved_active_lines:
            close_untracked_order_offer(
                db,
                sku_candidates={merchant_sku or "", external_product_id or ""},
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "total": len(products),
        "created_count": created,
        "updated_count": updated,
        "unchanged_count": unchanged,
        "linked_order_lines": linked_order_lines,
        "warning_count": len(warnings),
        "warnings": warnings,
        "feed_url": workspace_feed_url(db),
    }
