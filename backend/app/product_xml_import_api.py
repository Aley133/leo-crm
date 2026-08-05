from __future__ import annotations

from copy import deepcopy
from collections.abc import Iterable
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import unquote
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
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


def _element_text(element: ElementTree.Element, *names: str) -> str | None:
    accepted = {name.casefold() for name in names}
    for child in element.iter():
        if child is element or _local_name(child.tag) not in accepted:
            continue
        value = (child.text or "").strip()
        if value:
            return value
    return None


def _offer_identity(element: ElementTree.Element) -> str | None:
    attributes = {_local_name(key): value for key, value in element.attrib.items()}
    raw = next(
        (
            attributes.get(name)
            for name in ("sku", "id", "kaspi_product_id", "kaspiid", "productid", "code")
            if attributes.get(name)
        ),
        None,
    ) or _element_text(
        element,
        "merchantsku",
        "merchant_sku",
        "kaspi_product_id",
        "kaspiid",
        "productid",
        "code",
        "id",
        "sku",
    )
    return raw.strip() if raw and raw.strip() else None


def _offers_container(root: ElementTree.Element) -> ElementTree.Element:
    return next(
        (element for element in root.iter() if _local_name(element.tag) == "offers"),
        root,
    )


def _catalog_offers(root: ElementTree.Element) -> list[ElementTree.Element]:
    container = _offers_container(root)
    return [
        element
        for element in list(container)
        if _local_name(element.tag) in {"offer", "product", "item"}
    ]


def _merge_catalog_xml(existing_xml: str, incoming_xml: str) -> str:
    """Upsert incoming offers while preserving offers from earlier imports."""
    existing_root = ElementTree.fromstring(existing_xml.encode("utf-8"))
    incoming_root = ElementTree.fromstring(incoming_xml.encode("utf-8"))
    existing_container = _offers_container(existing_root)

    positions: dict[str, int] = {}
    anonymous: set[str] = set()
    for index, offer in enumerate(list(existing_container)):
        if _local_name(offer.tag) not in {"offer", "product", "item"}:
            continue
        identity = _offer_identity(offer)
        if identity:
            positions[identity] = index
        else:
            anonymous.add(ElementTree.tostring(offer, encoding="unicode"))

    for offer in _catalog_offers(incoming_root):
        replacement = deepcopy(offer)
        identity = _offer_identity(offer)
        if identity and identity in positions:
            existing_container[positions[identity]] = replacement
            continue
        if not identity:
            serialized = ElementTree.tostring(offer, encoding="unicode")
            if serialized in anonymous:
                continue
            anonymous.add(serialized)
        existing_container.append(replacement)
        if identity:
            positions[identity] = len(existing_container) - 1

    incoming_merchant = _element_text(incoming_root, "merchantid")
    if incoming_merchant:
        existing_merchant = next(
            (
                element
                for element in existing_root.iter()
                if _local_name(element.tag) == "merchantid"
            ),
            None,
        )
        if existing_merchant is None:
            tag = "merchantid"
            root_tag = str(existing_root.tag)
            if root_tag.startswith("{") and "}" in root_tag:
                tag = f"{root_tag.split('}', 1)[0]}}}merchantid"
            existing_merchant = ElementTree.Element(tag)
            existing_root.insert(0, existing_merchant)
        existing_merchant.text = incoming_merchant

    return ElementTree.tostring(
        existing_root,
        encoding="unicode",
        xml_declaration=True,
    )


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
    cumulative: bool = False,
) -> KaspiXmlFeed:
    incoming_xml = body.decode("utf-8-sig")
    incoming_merchant_id = _merchant_id(body)
    feed = db.scalar(
        select(KaspiXmlFeed)
        .order_by(KaspiXmlFeed.id.desc())
        .with_for_update()
        .limit(1)
    )
    if feed is None:
        feed = KaspiXmlFeed(
            merchant_id=incoming_merchant_id,
            source_filename=source_filename,
            source_xml=incoming_xml,
            generated_xml=incoming_xml,
            active=activate,
        )
        db.add(feed)
    else:
        if (
            cumulative
            and feed.merchant_id
            and incoming_merchant_id
            and feed.merchant_id != incoming_merchant_id
        ):
            raise HTTPException(
                status_code=422,
                detail="merchantId нового XML не совпадает с текущим каталогом",
            )
        source_xml = (
            _merge_catalog_xml(feed.source_xml, incoming_xml)
            if cumulative
            else incoming_xml
        )
        feed.merchant_id = incoming_merchant_id or feed.merchant_id
        feed.source_filename = source_filename or feed.source_filename
        feed.source_xml = source_xml
        feed.imported_at = func.now()
        if activate:
            feed.generated_xml = source_xml
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
    current_catalog_count = int(db.scalar(select(func.count(Product.id))) or 0)
    retained_count = max(current_catalog_count - len(existing_ids), 0)
    return {
        "total": len(products),
        "new_count": sum(1 for item in products if item.kaspi_product_id not in existing_ids),
        "existing_count": sum(1 for item in products if item.kaspi_product_id in existing_ids),
        "retained_count": retained_count,
        "catalog_total_after": current_catalog_count + sum(
            1 for item in products if item.kaspi_product_id not in existing_ids
        ),
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
    active_managed_product_ids: set[int] = set()
    existing_database_ids = [int(product.id) for product in existing.values()]
    for batch in _chunks(existing_database_ids):
        active_managed_product_ids.update(
            int(value)
            for value in db.scalars(
                select(DumpingPolicy.product_id).where(
                    DumpingPolicy.product_id.in_(batch),
                    DumpingPolicy.enabled.is_(True),
                    DumpingPolicy.auto_publish_xml.is_(True),
                )
            ).all()
        )

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
                    sale_enabled=item.available is not False,
                    sale_state_overridden=False,
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
                if (
                    item.available is not None
                    and not product.sale_state_overridden
                    and int(product.id) not in active_managed_product_ids
                    and bool(product.sale_enabled) is not item.available
                ):
                    product.sale_enabled = item.available
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

        feed = _store_feed_source(
            db,
            body=body,
            source_filename=source_filename,
            activate=True,
            cumulative=True,
        )
        feed.active = True
        managed_product_ids: set[int] = set()
        managed_product_ids.update(
            int(value)
            for value in db.scalars(
                select(DumpingPolicy.product_id).where(
                    DumpingPolicy.enabled.is_(True),
                    DumpingPolicy.auto_publish_xml.is_(True),
                )
            ).all()
        )
        manually_overridden_ids = {
            int(value)
            for value in db.scalars(
                select(Product.id).where(Product.sale_state_overridden.is_(True))
            ).all()
        }
        for product_id in sorted(managed_product_ids | manually_overridden_ids):
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
        "retained_count": max(
            int(db.scalar(select(func.count(Product.id))) or 0) - len(products),
            0,
        ),
        "catalog_total": int(db.scalar(select(func.count(Product.id))) or 0),
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
