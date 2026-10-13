from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from collections.abc import Iterable
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import unquote
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .auth import require_service_token
from .db import SessionLocal
from .dumping_models import DumpingPolicy, KaspiXmlFeed
from .fast_dumping_models import FastDumpingPolicy
from .dumping_service import (
    sync_product_inventory_to_feed,
    workspace_feed_url,
)
from .kaspi_xml_import import KaspiXmlProduct, parse_kaspi_products
from .models import (
    Product,
    ProductStatus,
)
from .order_line_product_linking import compound_master_id, link_all_matching_order_lines_for_products


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
            "image_url": item.image_url,
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
    # Kaspi may replace a numeric offer code with master_offer. Reuse the
    # existing row (and all its stock, policies and supplier links) only when
    # this import contains exactly one representation for that master.
    lookup = set(ids)
    for identity in ids:
        base = compound_master_id(identity)
        if base:
            lookup.add(base)
    rows: dict[int, Product] = {}
    for batch in _chunks(sorted(lookup)):
        for product in db.scalars(select(Product).where(or_(
            Product.kaspi_product_id.in_(batch), Product.merchant_sku.in_(batch),
        ))).all():
            rows[product.id] = product
    result: dict[str, Product] = {}
    for identity in ids:
        # Historical aliases can retain the same merchant SKU after their
        # warehouses are joined. The exact registry ID remains authoritative;
        # a repaired shared pool also resolves SKU-only imports deterministically.
        exact = [p for p in rows.values() if identity in (p.kaspi_product_id, p.merchant_sku)]
        owners = {p.inventory_owner_product_id or p.id for p in exact}
        if len(owners) == 1:
            owner = rows.get(next(iter(owners)))
            if owner is not None and (owner.merchant_sku == identity or
                    compound_master_id(identity) == owner.merchant_sku):
                result[identity] = owner
                continue
        primary = [p for p in rows.values() if p.kaspi_product_id == identity]
        if len(primary) == 1:
            result[identity] = primary[0]
            continue
        if len(exact) == 1:
            result[identity] = exact[0]
            continue
        if exact and len({p.inventory_owner_product_id or p.id for p in exact}) == 1:
            owner_id = exact[0].inventory_owner_product_id or exact[0].id
            result[identity] = next((p for p in exact if p.id == owner_id), exact[0])
            continue
        if exact:
            raise HTTPException(status_code=409, detail=f"Неоднозначные карточки SKU {identity}: требуется сверка склада")
        base = compound_master_id(identity)
        if not exact and base and sum(
            value == base or compound_master_id(value) == base for value in ids
        ) == 1:
            legacy = [p for p in rows.values() if base in (p.kaspi_product_id, p.merchant_sku)]
            if len(legacy) == 1:
                result[identity] = legacy[0]
    return result


def _rename_offer_skus(xml: str, renames: dict[str, str]) -> str:
    if not renames:
        return xml
    root = ElementTree.fromstring(xml)
    for offer in _catalog_offers(root):
        identity = _offer_identity(offer)
        if identity in renames:
            offer.set("sku", renames[identity])
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)


def _locked_import_feed(db: Session) -> KaspiXmlFeed | None:
    # All XML writers must acquire the feed before modifying products/policies.
    # Reversing this order deadlocks a concurrent inventory or Fast mirror write.
    return db.scalar(
        select(KaspiXmlFeed)
        .order_by(KaspiXmlFeed.id.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(1)
    )


def _merge_generated_xml(
    previous_xml: str,
    incoming_xml: str,
    *,
    fast_skus: set[str],
) -> str:
    """Merge once, retaining verified realtime fields on Fast-owned offers."""
    merged_root = ElementTree.fromstring(_merge_catalog_xml(previous_xml, incoming_xml))
    previous_root = ElementTree.fromstring(previous_xml)
    previous_offers = {
        _offer_identity(offer): offer for offer in _catalog_offers(previous_root)
    }
    controlled_fields = {"price", "cityprices", "availability", "availabilities"}
    for offer in _catalog_offers(merged_root):
        identity = _offer_identity(offer)
        previous = previous_offers.get(identity)
        if identity not in fast_skus or previous is None:
            continue
        for child in list(offer):
            if _local_name(child.tag) in controlled_fields:
                offer.remove(child)
        for child in previous:
            if _local_name(child.tag) in controlled_fields:
                offer.append(deepcopy(child))
    return ElementTree.tostring(merged_root, encoding="unicode", xml_declaration=True)


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
    feed = _locked_import_feed(db)
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
    existing_ids = set(_existing_products(db, ids))
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
    locked_feed = _locked_import_feed(db)
    previous_generated_xml = (
        locked_feed.generated_xml or locked_feed.source_xml if locked_feed else None
    )
    ids = list(dict.fromkeys(item.kaspi_product_id for item in products))
    existing = _existing_products(db, ids)
    active_managed_product_ids: set[int] = set()
    fast_product_ids: set[int] = set()
    fast_skus: set[str] = set()
    existing_database_ids = [int(product.id) for product in existing.values()]
    for batch in _chunks(existing_database_ids):
        fast_product_ids.update(
            int(value) for value in db.scalars(
                select(FastDumpingPolicy.product_id).where(
                    FastDumpingPolicy.product_id.in_(batch),
                    FastDumpingPolicy.enabled.is_(True),
                )
            ).all()
        )
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

    for product in existing.values():
        if int(product.id) in fast_product_ids:
            fast_skus.update(filter(None, (product.merchant_sku, product.kaspi_product_id)))

    created = 0
    updated = 0
    unchanged = 0
    linked_order_lines = 0
    try:
        stored_products: list[Product] = []
        renamed_skus: dict[str, str] = {}
        for item in products:
            product = existing.get(item.kaspi_product_id)
            if product is None:
                product = Product(
                    kaspi_product_id=item.kaspi_product_id,
                    merchant_sku=item.merchant_sku,
                    name=item.name,
                    brand=item.brand,
                    image_url=item.image_url,
                    status=ProductStatus.ACTIVE.value,
                    sale_enabled=item.available is not False,
                    sale_state_overridden=False,
                )
                db.add(product)
                existing[item.kaspi_product_id] = product
                created += 1
            else:
                changed = False
                old_sku = product.merchant_sku
                if old_sku and compound_master_id(old_sku) == item.merchant_sku:
                    # A stale archive must not resurrect the retired numeric offer.
                    renamed_skus[item.merchant_sku] = old_sku
                    item = replace(item, merchant_sku=old_sku)
                if (old_sku and item.merchant_sku and old_sku != item.merchant_sku
                        and compound_master_id(item.merchant_sku) == old_sku):
                    renamed_skus[old_sku] = item.merchant_sku
                    if int(product.id) in fast_product_ids:
                        fast_skus.add(item.merchant_sku)
                for field, value in (
                    ("merchant_sku", item.merchant_sku),
                    ("name", item.name),
                    ("brand", item.brand),
                    ("image_url", item.image_url),
                ):
                    if value is not None and getattr(product, field) != value:
                        setattr(product, field, value)
                        changed = True
                if (
                    item.available is not None
                    and not product.sale_state_overridden
                    and int(product.id) not in active_managed_product_ids
                    and int(product.id) not in fast_product_ids
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

        if renamed_skus:
            body = _rename_offer_skus(body.decode("utf-8-sig"), renamed_skus).encode("utf-8")
        if locked_feed is not None and renamed_skus:
            locked_feed.source_xml = _rename_offer_skus(locked_feed.source_xml, renamed_skus)
        if previous_generated_xml and renamed_skus:
            previous_generated_xml = _rename_offer_skus(previous_generated_xml, renamed_skus)
        feed = _store_feed_source(
            db,
            body=body,
            source_filename=source_filename,
            activate=True,
            cumulative=True,
        )
        feed.active = True
        if previous_generated_xml:
            feed.generated_xml = _merge_generated_xml(
                previous_generated_xml,
                body.decode("utf-8-sig"),
                fast_skus=fast_skus,
            )
        # Reimport is not an inventory event. Unaffected offers already contain
        # their authoritative overlay; Fast-owned offers keep their verified
        # mirror without cancelling applies or enqueueing scans for the catalog.
        manually_overridden_ids = {
            int(product.id) for product in stored_products if product.sale_state_overridden
        }
        for product_id in sorted(
            (active_managed_product_ids | manually_overridden_ids) - fast_product_ids
        ):
            sync_product_inventory_to_feed(
                db,
                product_id=product_id,
                reason="xml_source_reimported",
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    catalog_total = int(db.scalar(select(func.count(Product.id))) or 0)
    return {
        "total": len(products),
        "created_count": created,
        "updated_count": updated,
        "unchanged_count": unchanged,
        "retained_count": max(
            catalog_total - len(products),
            0,
        ),
        "catalog_total": catalog_total,
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
