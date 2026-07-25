from __future__ import annotations

from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import KaspiXmlFeed
from .kaspi_xml_import import KaspiXmlProduct, parse_kaspi_products
from .models import Product, ProductStatus
from .order_line_product_linking import link_all_matching_order_lines_for_products


router = APIRouter(
    prefix="/api/product-registry/imports/xml",
    tags=["product-registry"],
    dependencies=[Depends(require_service_token)],
)


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


@router.post("/preview")
async def preview_xml_import(request: Request, db: Session = Depends(get_db)) -> dict:
    _body, products, warnings = await _read_products(request)
    ids = [item.kaspi_product_id for item in products]
    existing_ids = set(
        db.scalars(select(Product.kaspi_product_id).where(Product.kaspi_product_id.in_(ids))).all()
    )
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
    ids = [item.kaspi_product_id for item in products]
    existing = {
        item.kaspi_product_id: item
        for item in db.scalars(select(Product).where(Product.kaspi_product_id.in_(ids))).all()
    }

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

        xml_text = body.decode("utf-8-sig")
        feed = db.scalar(select(KaspiXmlFeed).order_by(KaspiXmlFeed.id.desc()).limit(1))
        if feed is None:
            feed = KaspiXmlFeed(
                merchant_id=_merchant_id(body),
                source_xml=xml_text,
                generated_xml=xml_text,
                active=True,
            )
            db.add(feed)
        else:
            feed.merchant_id = _merchant_id(body) or feed.merchant_id
            feed.source_xml = xml_text
            feed.generated_xml = xml_text
            feed.active = True

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
        "feed_url": "/feeds/kaspi/catalog.xml",
    }
