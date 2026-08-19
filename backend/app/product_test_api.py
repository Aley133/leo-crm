from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db, get_unscoped_db
from .dumping_models import KaspiXmlFeed
from .fast_dumping_agent_api import FastAgentIdentity, _validate_workspace_merchant
from .kaspi_xml_schema import catalog_store_id, ensure_offer_availability, repair_kaspi_catalog_tree
from .models import Product
from .product_images import normalize_product_image_url
from .product_test_models import ProductTestItem, ProductTestJob
from .workspace_context import workspace_context


DEFAULT_CITY_ID = "196220100"
DEFAULT_ZONE_ID = "Magnum_ZONE1"
LEASE_SECONDS = 180
MAX_XML_BYTES = 25 * 1024 * 1024


class ProductTestInspectRequest(BaseModel):
    reference: str = Field(min_length=6, max_length=2000)
    city_id: str = Field(default=DEFAULT_CITY_ID, min_length=1, max_length=32)
    zone_id: str = Field(default=DEFAULT_ZONE_ID, min_length=1, max_length=64)


class ProductTestUpdate(BaseModel):
    test_price_kzt: Decimal | None = Field(default=None, gt=0)
    preorder_days: int | None = Field(default=None, ge=0, le=365)
    stock_count: int | None = Field(default=None, ge=0, le=1_000_000)
    city_id: str | None = Field(default=None, min_length=1, max_length=32)
    zone_id: str | None = Field(default=None, min_length=1, max_length=64)
    supplier_url: str | None = Field(default=None, max_length=4000)
    active: bool | None = None


class ProductTestAgentResult(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    workspace_id: int = Field(ge=1)
    lease_token: str = Field(min_length=16, max_length=64)
    status: str = Field(pattern="^(succeeded|failed)$")
    result: dict = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=4000)


router = APIRouter(
    prefix="/api/product-test",
    tags=["product-test"],
    dependencies=[Depends(require_service_token)],
)
agent_router = APIRouter(
    prefix="/api/product-test-agent",
    tags=["product-test-agent"],
    dependencies=[Depends(require_service_token)],
)


def _now() -> datetime:
    return datetime.now(UTC)


def _money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result > 0 else None


def _item_payload(item: ProductTestItem) -> dict:
    return {
        "id": item.id,
        "input_reference": item.input_reference,
        "kaspi_product_id": item.kaspi_product_id,
        "merchant_sku": item.merchant_sku,
        "name": item.name,
        "brand": item.brand,
        "image_url": item.image_url,
        "kaspi_url": item.kaspi_url,
        "supplier_url": item.supplier_url,
        "observed_price_kzt": item.observed_price_kzt,
        "test_price_kzt": item.test_price_kzt,
        "preorder_days": item.preorder_days,
        "stock_count": item.stock_count,
        "city_id": item.city_id,
        "zone_id": item.zone_id,
        "offers": item.offers_json,
        "active": item.active,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _job_payload(job: ProductTestJob) -> dict:
    return {
        "id": job.id,
        "reference": job.input_reference,
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _qualified_tag(parent: ElementTree.Element, local_name: str) -> str:
    tag = str(parent.tag)
    if tag.startswith("{") and "}" in tag:
        return f"{tag.split('}', 1)[0]}}}{local_name}"
    return local_name


def _direct_child(parent: ElementTree.Element, name: str) -> ElementTree.Element | None:
    wanted = name.casefold()
    return next((node for node in list(parent) if _local_name(node.tag) == wanted), None)


def _offers_node(root: ElementTree.Element) -> ElementTree.Element:
    node = next((candidate for candidate in root.iter() if _local_name(candidate.tag) == "offers"), None)
    return node if node is not None else ElementTree.SubElement(root, _qualified_tag(root, "offers"))


def _matching_offer(root: ElementTree.Element, sku: str) -> ElementTree.Element | None:
    for node in root.iter():
        if _local_name(node.tag) != "offer":
            continue
        if str(node.attrib.get("sku") or node.attrib.get("id") or "").strip() == sku:
            return node
    return None


def _set_text_child(parent: ElementTree.Element, name: str, value: str | None) -> None:
    text = str(value or "").strip()
    if not text:
        return
    node = _direct_child(parent, name)
    if node is None:
        node = ElementTree.Element(_qualified_tag(parent, name))
        insert_at = next(
            (index for index, child in enumerate(list(parent)) if _local_name(child.tag) in {"availabilities", "availability", "cityprices"}),
            len(list(parent)),
        )
        parent.insert(insert_at, node)
    node.text = text


def _set_city_price(offer: ElementTree.Element, city_id: str, price: Decimal) -> None:
    cityprices = _direct_child(offer, "cityprices")
    if cityprices is None:
        cityprices = ElementTree.SubElement(offer, _qualified_tag(offer, "cityprices"))
    prices = [node for node in list(cityprices) if _local_name(node.tag) == "cityprice"]
    if not prices:
        prices = [ElementTree.SubElement(cityprices, _qualified_tag(cityprices, "cityprice"), {"cityId": city_id})]
    text = str(int(price)) if price == price.to_integral_value() else format(price, "f")
    for node in prices:
        if not str(node.attrib.get("cityId") or "").strip():
            node.set("cityId", city_id)
        node.text = text


def build_product_test_xml(source_xml: str, items: list[ProductTestItem]) -> bytes:
    raw = source_xml.encode("utf-8")
    if not raw or len(raw) > MAX_XML_BYTES:
        raise ValueError("Базовый XML отсутствует или превышает 25 МБ")
    if b"<!DOCTYPE" in raw[:2048].upper() or b"<!ENTITY" in raw[:2048].upper():
        raise ValueError("DOCTYPE и внешние XML-сущности запрещены")
    root = ElementTree.fromstring(raw)
    repair_kaspi_catalog_tree(root)
    store_id = catalog_store_id(root)
    if not store_id:
        raise ValueError("В активном XML не найден storeId")
    for item in items:
        if not item.active or item.test_price_kzt is None:
            continue
        offer = _matching_offer(root, item.merchant_sku)
        if offer is None:
            offers = _offers_node(root)
            offer = ElementTree.SubElement(
                offers,
                _qualified_tag(offers, "offer"),
                {"sku": item.merchant_sku},
            )
        else:
            offer.set("sku", item.merchant_sku)
        _set_text_child(offer, "model", item.name)
        _set_text_child(offer, "brand", item.brand)
        availability = ensure_offer_availability(offer)
        availability.set("available", "yes")
        availability.set("storeId", store_id)
        availability.set("preOrder", str(max(0, item.preorder_days)))
        availability.set("stockCount", str(max(0, item.stock_count)))
        _set_city_price(offer, item.city_id or DEFAULT_CITY_ID, item.test_price_kzt)
    repaired = repair_kaspi_catalog_tree(root)
    try:
        ElementTree.indent(repaired, space="  ")
    except AttributeError:
        pass
    return ElementTree.tostring(repaired, encoding="utf-8", xml_declaration=True)


@router.get("")
def read_product_test_state(db: Session = Depends(get_db)) -> dict:
    items = list(db.scalars(select(ProductTestItem).order_by(ProductTestItem.updated_at.desc(), ProductTestItem.id.desc())).all())
    jobs = list(db.scalars(select(ProductTestJob).order_by(ProductTestJob.id.desc()).limit(20)).all())
    feed = db.scalar(select(KaspiXmlFeed).where(KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1))
    return {
        "items": [_item_payload(item) for item in items],
        "jobs": [_job_payload(job) for job in jobs],
        "feed": None if feed is None else {"id": feed.id, "source_filename": feed.source_filename, "merchant_id": feed.merchant_id},
        "defaults": {"city_id": DEFAULT_CITY_ID, "zone_id": DEFAULT_ZONE_ID},
    }


@router.post("/inspect", status_code=202)
def queue_product_inspection(payload: ProductTestInspectRequest, db: Session = Depends(get_db)) -> dict:
    reference = payload.reference.strip()
    pending = db.scalar(
        select(ProductTestJob).where(
            ProductTestJob.input_reference == reference,
            ProductTestJob.status.in_(("queued", "leased")),
        ).order_by(ProductTestJob.id.desc()).limit(1)
    )
    if pending is None:
        pending = ProductTestJob(
            input_reference=reference,
            city_id=payload.city_id.strip(),
            zone_id=payload.zone_id.strip(),
            status="queued",
            result_json={},
        )
        db.add(pending)
        db.commit()
        db.refresh(pending)
    return _job_payload(pending)


@router.patch("/items/{item_id}")
def update_product_test_item(item_id: int, payload: ProductTestUpdate, db: Session = Depends(get_db)) -> dict:
    item = db.get(ProductTestItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Тестовый товар не найден")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return _item_payload(item)


@router.get("/xml")
def download_product_test_xml(db: Session = Depends(get_db)) -> Response:
    feed = db.scalar(select(KaspiXmlFeed).where(KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None:
        raise HTTPException(status_code=409, detail="Сначала загрузите основной XML в разделе Товары")
    items = list(db.scalars(select(ProductTestItem).where(ProductTestItem.active.is_(True)).order_by(ProductTestItem.id)).all())
    try:
        content = build_product_test_xml(feed.generated_xml or feed.source_xml, items)
    except (ElementTree.ParseError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/xml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leo-product-test.xml"'},
    )


@agent_router.post("/claim")
def claim_product_test_job(payload: FastAgentIdentity, db: Session = Depends(get_unscoped_db)) -> dict:
    try:
        _validate_workspace_merchant(db, workspace_id=payload.workspace_id, merchant_uid=payload.merchant_uid)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    with workspace_context(payload.workspace_id):
        now = _now()
        job = db.scalar(
            select(ProductTestJob)
            .where(
                ProductTestJob.workspace_id == payload.workspace_id,
                or_(ProductTestJob.status == "queued", (ProductTestJob.status == "leased") & (ProductTestJob.lease_until < now)),
            )
            .order_by(ProductTestJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return {"job": None}
        job.status = "leased"
        job.agent_id = payload.agent_id
        job.lease_token = secrets.token_hex(16)
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        db.commit()
        return {
            "job": {
                "id": job.id,
                "reference": job.input_reference,
                "city_id": job.city_id,
                "zone_id": job.zone_id,
                "lease_token": job.lease_token,
            }
        }


@agent_router.post("/jobs/{job_id}/complete")
def complete_product_test_job(job_id: int, payload: ProductTestAgentResult, db: Session = Depends(get_unscoped_db)) -> dict:
    with workspace_context(payload.workspace_id):
        job = db.scalar(
            select(ProductTestJob).where(
                ProductTestJob.id == job_id,
                ProductTestJob.workspace_id == payload.workspace_id,
            ).with_for_update()
        )
        if job is None:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        if job.status != "leased" or job.agent_id != payload.agent_id or job.lease_token != payload.lease_token:
            raise HTTPException(status_code=409, detail="Задание уже завершено или аренда недействительна")
        job.result_json = payload.result
        job.error_code = payload.error_code
        job.error_message = payload.error_message
        job.completed_at = _now()
        if payload.status == "failed":
            job.status = "failed"
            db.commit()
            return _job_payload(job)

        result = payload.result
        kaspi_id = str(result.get("kaspi_product_id") or "").strip()[:64]
        merchant_sku = str(result.get("merchant_sku") or kaspi_id).strip()[:128]
        name = str(result.get("product_name") or kaspi_id).strip()[:500]
        kaspi_url = str(result.get("product_url") or "").strip()[:4000]
        if not kaspi_id or not merchant_sku or not name or not kaspi_url:
            raise HTTPException(status_code=422, detail="Agent вернул неполные данные карточки")
        item = db.scalar(
            select(ProductTestItem).where(
                ProductTestItem.workspace_id == payload.workspace_id,
                ProductTestItem.merchant_sku == merchant_sku,
            ).with_for_update()
        )
        observed = _money(result.get("page_visible_price_kzt"))
        if item is None:
            item = ProductTestItem(
                workspace_id=payload.workspace_id,
                input_reference=job.input_reference,
                kaspi_product_id=kaspi_id,
                merchant_sku=merchant_sku,
                name=name,
                brand=(str(result.get("brand") or "").strip()[:255] or None),
                image_url=normalize_product_image_url(result.get("image_url")),
                kaspi_url=kaspi_url,
                observed_price_kzt=observed,
                test_price_kzt=observed,
                city_id=job.city_id,
                zone_id=job.zone_id,
                offers_json=result.get("offers") if isinstance(result.get("offers"), dict) else {},
            )
            db.add(item)
        else:
            item.input_reference = job.input_reference
            item.kaspi_product_id = kaspi_id
            item.name = name
            item.brand = str(result.get("brand") or "").strip()[:255] or None
            item.image_url = normalize_product_image_url(result.get("image_url"))
            item.kaspi_url = kaspi_url
            item.observed_price_kzt = observed
            item.offers_json = result.get("offers") if isinstance(result.get("offers"), dict) else {}
            item.active = True
        product = db.scalar(
            select(Product).where(
                Product.workspace_id == payload.workspace_id,
                or_(Product.kaspi_product_id == kaspi_id, Product.merchant_sku == merchant_sku),
            ).limit(1)
        )
        if product is not None and item.image_url:
            product.image_url = item.image_url
        job.status = "succeeded"
        db.commit()
        db.refresh(item)
        return {"job": _job_payload(job), "item": _item_payload(item)}
