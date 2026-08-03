from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import unquote
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .auth import require_service_token
from .db import SessionLocal
from .dumping_models import DumpingPolicy, KaspiXmlFeed
from .dumping_service import (
    sync_product_inventory_to_feed,
    workspace_feed_url,
)
from .kaspi_xml_import import KaspiXmlProduct, parse_kaspi_products
from .models import (
    Product,
    ProductStatus,
)
from .order_line_product_linking import link_all_matching_order_lines_for_products


router = APIRouter(
    prefix="/api/product-registry/imports/xml",
    tags=["product-registry"],
    dependencies=[Depends(require_service_token)],
)

_LOOKUP_BATCH_SIZE = 400
_Result = TypeVar("_Result")
_LookupValue = TypeVar("_LookupValue")


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


def _parse_products(body: bytes) -> tuple[list[KaspiXmlProduct], list[str]]:
    try:
        products, warnings = parse_kaspi_products(body)
        return products, warnings
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _with_session(operation: Callable[[Session], _Result]) -> _Result:
    """Create, use and close a DB session inside the same worker thread."""
    with SessionLocal() as db:
        return operation(db)


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


def _chunks(
    values: list[_LookupValue],
    size: int = _LOOKUP_BATCH_SIZE,
) -> Iterable[list[_LookupValue]]:
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
    feed = db.scalar(
        select(KaspiXmlFeed)
        .order_by(KaspiXmlFeed.id.desc())
        .with_for_update()
        .limit(1)
    )
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


def _retain_xml_source(
    body: bytes,
    *,
    source_filename: str | None,
    db: Session,
) -> dict:
    products, warnings = _parse_products(body)
    try:
        feed = _store_feed_source(
            db,
            body=body,
            source_filename=source_filename,
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


@router.post("/retain-source")
async def retain_xml_source(request: Request) -> dict:
    body = await request.body()
    source_filename = _source_filename(request)
    return await run_in_threadpool(
        _with_session,
        lambda db: _retain_xml_source(
            body,
            source_filename=source_filename,
            db=db,
        ),
    )


def _preview_xml_import(body: bytes, *, db: Session) -> dict:
    products, warnings = _parse_products(body)
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


@router.post("/preview")
async def preview_xml_import(request: Request) -> dict:
    body = await request.body()
    return await run_in_threadpool(
        _with_session,
        lambda db: _preview_xml_import(body, db=db),
    )


def _commit_xml_import(
    body: bytes,
    *,
    source_filename: str | None,
    db: Session,
) -> dict:
    products, warnings = _parse_products(body)
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
            source_filename=source_filename,
            activate=True,
        )
        feed.active = True
        managed_product_ids: set[int] = set()
        stored_product_ids = [int(product.id) for product in stored_products]
        for batch in _chunks(stored_product_ids):
            managed_product_ids.update(
                int(value)
                for value in db.scalars(
                    select(DumpingPolicy.product_id).where(
                        DumpingPolicy.product_id.in_(batch),
                        DumpingPolicy.enabled.is_(True),
                        DumpingPolicy.auto_publish_xml.is_(True),
                    )
                ).all()
            )
        for product_id in sorted(int(value) for value in managed_product_ids):
            sync_product_inventory_to_feed(
                db,
                product_id=product_id,
                reason="xml_source_reimported",
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


@router.post("/commit")
async def commit_xml_import(request: Request) -> dict:
    body = await request.body()
    source_filename = _source_filename(request)
    return await run_in_threadpool(
        _with_session,
        lambda db: _commit_xml_import(
            body,
            source_filename=source_filename,
            db=db,
        ),
    )
