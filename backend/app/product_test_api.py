from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Literal
from xml.etree import ElementTree
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_dispatch import queue_browser_target_now
from .db import get_db, get_unscoped_db
from .dumping_models import KaspiXmlFeed
from .fast_dumping_models import FastDumpingPolicy
from .fast_dumping_service import ensure_state, queue_scan
from .fast_dumping_agent_api import FastAgentIdentity, _validate_workspace_merchant
from .kaspi_xml_schema import catalog_store_id, ensure_offer_availability, repair_kaspi_catalog_tree
from .models import Product, ProductStatus
from .monitoring import MonitorStatus, MonitorTarget, SupplierOfferObservation, SupplierOfferState
from .offer_contracts import offer_fingerprint
from .product_images import normalize_product_image_url
from .product_test_models import ProductTestItem, ProductTestJob, ProductTestSettings
from .product_test_pricing import choose_initial_offer_price
from .suppliers import ProductBinding, Supplier, SupplierProduct
from .workspace_context import current_workspace_id, workspace_context
DEFAULT_CITY_ID = "196220100"
DEFAULT_ZONE_ID = "Magnum_ZONE1"
MAX_XML_BYTES = 25 * 1024 * 1024
PRODUCT_TEST_LEASE_SECONDS = 180
PRODUCT_TEST_HISTORY_LIMIT = 40
PRODUCT_TEST_AGENT_KIND = "product_test"
PRODUCT_TEST_AGENT_ONLINE_SECONDS = 50
PRODUCT_TEST_SUCCESS_VISIBLE_SECONDS = 15 * 60
PRODUCT_TEST_CATEGORY_ERROR_VISIBLE_SECONDS = 3 * 60
PRODUCT_TEST_NEW_CARD_FIRST_CHECK_SECONDS = 60
PRODUCT_TEST_NEW_CARD_RECHECK_SECONDS = 5 * 60
PRODUCT_TEST_NEW_CARD_WATCH_SECONDS = 7 * 24 * 60 * 60
PRODUCT_TEST_CATEGORY_REJECTION_CODES = frozenset({
    "VALIDATE_CHOOSE_REJECTED",
    "VALIDATE_PRICE_STOCK_REJECTED",
})


class ProductTestInspectRequest(BaseModel):
    reference: str = Field(min_length=6, max_length=2000)
    city_id: str = Field(default=DEFAULT_CITY_ID, min_length=1, max_length=32)
    zone_id: str = Field(default=DEFAULT_ZONE_ID, min_length=1, max_length=64)


class ProductDiscoveryRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    target_new: int | None = Field(default=None, ge=1, le=100)
    mode: Literal["full", "popular"] = "full"
    minimum_reviews: int = Field(default=50, ge=0, le=10_000_000)
    maximum_sellers: int = Field(default=5, ge=1, le=100)


class ProductTestSettingsUpdate(BaseModel):
    target_new: int | None = Field(default=None, ge=1, le=100)
    max_kaspi_scan: int | None = Field(default=None, ge=1, le=1000)
    max_ozon_queries: int | None = Field(default=None, ge=1, le=8)
    image_verify: bool | None = None
    stock_count: int | None = Field(default=None, ge=1, le=1000000)
    preorder_buffer_days: int | None = Field(default=None, ge=0, le=30)
    minimum_profit_kzt: Decimal | None = Field(default=None, ge=0, le=100000000)
    undercut_step_kzt: int | None = Field(default=None, ge=1, le=10000)
    allow_price_raise: bool | None = None
    max_undercut_gap_percent: Decimal | None = Field(default=None, gt=0, le=100)
    scan_interval_seconds: int | None = Field(default=None, ge=300, le=3600)
    delivery_price_premium_kzt: int | None = Field(default=None, ge=0, le=100000)
    delivery_advantage_days: int | None = Field(default=None, ge=1, le=30)
    preorder_target_position: int | None = Field(default=None, ge=1, le=50)
    city_id: str | None = Field(default=None, min_length=1, max_length=32)
    zone_id: str | None = Field(default=None, min_length=1, max_length=64)


class SupplierUrlRequest(BaseModel):
    supplier_url: str = Field(min_length=12, max_length=4000, pattern=r"^https://(?:[^/]+\.)?ozon\.(?:ru|kz)/")


class ProductTestNewCardRequest(BaseModel):
    supplier_url: str = Field(min_length=12, max_length=4000, pattern=r"^https://(?:[^/]+\.)?ozon\.(?:ru|kz)/")


class ProductTestNewCardCategoryRequest(BaseModel):
    category: str = Field(min_length=1, max_length=255)


class ProductTestNewCardUpdate(BaseModel):
    # A draft must accept intermediate empty values while the operator is
    # typing.  Final Product Import validation below still requires every
    # mandatory value, but autosave must never reject and lose a partially
    # edited field merely because it is temporarily blank.
    sku: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=1024)
    brand: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    weight: Decimal | None = Field(default=None, gt=0, le=10000)
    category: str | None = Field(default=None, max_length=255)
    category_title: str | None = Field(default=None, max_length=500)
    attributes: list[dict] | None = Field(default=None, max_length=300)
    images: list[str] | None = Field(default=None, max_length=20)


class ProductTestUpdate(BaseModel):
    test_price_kzt: Decimal | None = Field(default=None, gt=0)
    preorder_days: int | None = Field(default=None, ge=1, le=365)
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


class ProductTestAgentIdentity(FastAgentIdentity):
    # Optional only for a graceful cut-over: Fast Agent 1.2.0 still calls the
    # old endpoint. It receives an empty queue instead of repeatedly logging a
    # validation error, while only the dedicated agent can receive work.
    agent_kind: str | None = Field(default=None, max_length=64)


class ProductTestAgentHeartbeat(ProductTestAgentIdentity):
    status: str = Field(default="online", max_length=32)


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

_PRODUCT_TEST_HEARTBEATS: dict[tuple[int, str], dict] = {}
_PRODUCT_TEST_HEARTBEATS_LOCK = Lock()
_MAX_PRODUCT_TEST_HEARTBEATS = 32


def _now() -> datetime:
    return datetime.now(UTC)


def _touch_product_test_agent(
    payload: ProductTestAgentIdentity,
    *,
    status: str = "online",
) -> dict:
    record = {
        "agent_id": payload.agent_id,
        "agent_kind": payload.agent_kind,
        "workspace_id": payload.workspace_id,
        "hostname": payload.hostname,
        "platform": payload.platform,
        "version": payload.version,
        "concurrency": payload.concurrency,
        "merchant_uid": payload.merchant_uid,
        "status": status,
        "last_seen_at": _now(),
    }
    with _PRODUCT_TEST_HEARTBEATS_LOCK:
        _PRODUCT_TEST_HEARTBEATS[(payload.workspace_id, payload.agent_id)] = record
        if len(_PRODUCT_TEST_HEARTBEATS) > _MAX_PRODUCT_TEST_HEARTBEATS:
            oldest = sorted(
                _PRODUCT_TEST_HEARTBEATS,
                key=lambda key: _PRODUCT_TEST_HEARTBEATS[key]["last_seen_at"],
            )[: len(_PRODUCT_TEST_HEARTBEATS) - _MAX_PRODUCT_TEST_HEARTBEATS]
            for key in oldest:
                _PRODUCT_TEST_HEARTBEATS.pop(key, None)
    return record


def _product_test_agent_status(workspace_id: int) -> dict:
    checked_at = _now()
    with _PRODUCT_TEST_HEARTBEATS_LOCK:
        agents = [
            dict(record)
            for (record_workspace, _agent_id), record in _PRODUCT_TEST_HEARTBEATS.items()
            if record_workspace == workspace_id
        ]
    agents.sort(key=lambda item: item["last_seen_at"], reverse=True)
    cutoff = checked_at - timedelta(seconds=PRODUCT_TEST_AGENT_ONLINE_SECONDS)
    for item in agents:
        item["online"] = item["last_seen_at"] >= cutoff
    return {
        "workspace_id": workspace_id,
        "online": any(item["online"] for item in agents),
        "agents": agents,
        "checked_at": checked_at,
    }


def _money(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result > 0 else None


def _external_https_url(value: object, *, max_length: int = 4000) -> str | None:
    raw = str(value or "").strip()
    return raw[:max_length] if raw.startswith("https://") else None


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
        "status": item.status,
        "product_id": item.product_id,
        "fast_dumping_policy_id": item.fast_dumping_policy_id,
        "last_error": item.last_error,
        "added_at": item.added_at,
        "active": item.active,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _kaspi_submission(item: ProductTestItem) -> dict:
    details = item.offers_json if isinstance(item.offers_json, dict) else {}
    submission = details.get("kaspi_submission")
    return dict(submission) if isinstance(submission, dict) else {}


def _set_kaspi_submission(item: ProductTestItem, **values: object) -> dict:
    details = dict(item.offers_json or {})
    submission = _kaspi_submission(item)
    submission.update(values)
    details["kaspi_submission"] = submission
    item.offers_json = details
    return submission


def _is_new_card_item(item: ProductTestItem) -> bool:
    details = item.offers_json if isinstance(item.offers_json, dict) else {}
    return details.get("mode") == "new_card" and isinstance(details.get("new_card"), dict)


def _new_card_draft(item: ProductTestItem) -> dict:
    details = item.offers_json if isinstance(item.offers_json, dict) else {}
    draft = details.get("new_card")
    return dict(draft) if isinstance(draft, dict) else {}


def _set_new_card_draft(item: ProductTestItem, draft: dict) -> None:
    details = dict(item.offers_json or {})
    details["mode"] = "new_card"
    details["new_card"] = draft
    item.offers_json = details


def _attribute_key(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("code") or "").strip().casefold()


def _operator_attribute_updates(current: list[dict], incoming: list[dict]) -> list[dict]:
    """Mark values changed in the CRM form as authoritative operator input."""

    current_by_code = {
        key: dict(row)
        for row in current
        if (key := _attribute_key(row))
    }
    output: list[dict] = []
    for raw in incoming[:300]:
        if not isinstance(raw, dict):
            continue
        key = _attribute_key(raw)
        previous = current_by_code.get(key, {})
        row = {**previous, **dict(raw)}
        if previous.get("manual_override") or row.get("value") != previous.get("value"):
            row["manual_override"] = True
        else:
            row.pop("manual_override", None)
        output.append(row)
    return output


def _preserve_operator_attribute_values(current: list[dict], mapped: list[dict]) -> list[dict]:
    """Merge a fresh Kaspi mapping without overwriting saved manual values."""

    current_by_code = {
        key: dict(row)
        for row in current
        if (key := _attribute_key(row))
    }
    output: list[dict] = []
    for raw in mapped[:300]:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        previous = current_by_code.get(_attribute_key(row))
        if previous and previous.get("manual_override"):
            row["value"] = previous.get("value")
            row["manual_override"] = True
            row["manual_source_name"] = "Введено вручную"
        output.append(row)
    return output


def _new_card_draft_errors(draft: dict) -> list[str]:
    errors: list[str] = []
    required = {
        "sku": "SKU",
        "title": "Название",
        "brand": "Бренд",
        "category": "Категория Kaspi",
    }
    for key, label in required.items():
        if not str(draft.get(key) or "").strip():
            errors.append(f"Не заполнено: {label}")
    description = str(draft.get("description") or "").strip()
    if len(description) < 100 or len(description) > 1024:
        errors.append("Описание должно содержать от 100 до 1024 символов")
    images = [value for value in list(draft.get("images") or []) if _external_https_url(value)]
    if not images:
        errors.append("Выберите хотя бы одно фото Ozon")
    for row in list(draft.get("attributes") or []):
        if not isinstance(row, dict) or not row.get("required"):
            continue
        value = row.get("value")
        if value in (None, "", []):
            errors.append(f"Обязательное поле Kaspi: {row.get('title') or row.get('code')}")
    return errors[:100]


def _new_card_job_draft(item: ProductTestItem) -> dict:
    draft = _new_card_draft(item)
    return {
        key: draft.get(key)
        for key in (
            "source_url", "sku", "title", "brand", "description", "weight",
            "category", "category_title", "attributes", "images",
        )
    }


def _submission_is_visible(item: ProductTestItem, *, now: datetime) -> bool:
    submission = _kaspi_submission(item)
    if not submission:
        return False
    raw_hide_after = str(submission.get("hide_after") or "").strip()
    if not raw_hide_after:
        return True
    try:
        hide_after = datetime.fromisoformat(raw_hide_after.replace("Z", "+00:00"))
    except ValueError:
        return True
    if hide_after.tzinfo is None:
        hide_after = hide_after.replace(tzinfo=UTC)
    return hide_after > now


def _category_rejection_hide_after(*, error_code: object, error_message: object) -> str | None:
    text = f"{error_code or ''} {error_message or ''}".upper()
    if not any(code in text for code in PRODUCT_TEST_CATEGORY_REJECTION_CODES):
        return None
    return (_now() + timedelta(seconds=PRODUCT_TEST_CATEGORY_ERROR_VISIBLE_SECONDS)).isoformat()


def _job_payload(job: ProductTestJob) -> dict:
    return {
        "id": job.id,
        "reference": job.input_reference,
        "job_type": job.job_type,
        "item_id": job.item_id,
        "status": job.status,
        "agent_id": job.agent_id,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": job.result_json if isinstance(job.result_json, dict) else {},
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
    }


def _settings(db: Session, workspace_id: int) -> ProductTestSettings:
    row = db.scalar(select(ProductTestSettings).where(ProductTestSettings.workspace_id == workspace_id))
    if row is None:
        row = ProductTestSettings(workspace_id=workspace_id)
        db.add(row)
        db.flush()
    return row


def _refresh_upload_plan(item: ProductTestItem, settings: ProductTestSettings) -> dict | None:
    """Keep the operator preview and the eventual create job on one calculation."""

    details = dict(item.offers_json or {})
    supplier = details.get("supplier") if isinstance(details.get("supplier"), dict) else {}
    supplier_cost = _money(supplier.get("supplier_price_kzt"))
    if not supplier.get("validated") or supplier_cost is None:
        item.test_price_kzt = None
        item.preorder_days = max(1, int(item.preorder_days or 0))
        details.pop("initial_pricing", None)
        item.offers_json = details
        return None
    pricing = choose_initial_offer_price(
        supplier_cost_kzt=supplier_cost,
        minimum_profit_kzt=Decimal(settings.minimum_profit_kzt),
        competitor_price_kzt=_money(item.observed_price_kzt),
        undercut_step_kzt=settings.undercut_step_kzt,
    )
    try:
        delivery_days = max(0, int(supplier.get("supplier_delivery_days") or 0))
    except (TypeError, ValueError):
        delivery_days = 0
    item.test_price_kzt = pricing.price_kzt
    item.stock_count = settings.stock_count
    item.preorder_days = max(1, delivery_days + settings.preorder_buffer_days)
    details["initial_pricing"] = {
        "price_kzt": format(pricing.price_kzt, "f"),
        "safe_floor_kzt": format(pricing.safe_floor_kzt, "f"),
        "competitor_price_kzt": (
            None
            if pricing.competitor_price_kzt is None
            else format(pricing.competitor_price_kzt, "f")
        ),
        "status": pricing.status,
        "preorder_days": item.preorder_days,
        "supplier_delivery_days": delivery_days,
    }
    item.offers_json = details
    return details["initial_pricing"]


def _settings_payload(row: ProductTestSettings) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "target_new", "max_kaspi_scan", "max_ozon_queries", "image_verify",
            "stock_count", "preorder_buffer_days", "minimum_profit_kzt",
            "undercut_step_kzt", "allow_price_raise", "max_undercut_gap_percent",
            "scan_interval_seconds", "delivery_price_premium_kzt",
            "delivery_advantage_days", "preorder_target_position", "city_id", "zone_id",
        )
    }


def _queue_job(
    db: Session,
    *,
    workspace_id: int,
    job_type: str,
    reference: str,
    city_id: str,
    zone_id: str,
    item_id: int | None = None,
    options: dict | None = None,
    not_before: datetime | None = None,
) -> ProductTestJob:
    running = db.scalar(
        select(ProductTestJob).where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.job_type == job_type,
            ProductTestJob.input_reference == reference,
            ProductTestJob.status.in_(("queued", "leased")),
        ).limit(1)
    )
    if running is not None:
        raise HTTPException(status_code=409, detail="Такое задание уже выполняется локальным Agent")
    job = ProductTestJob(
        workspace_id=workspace_id,
        job_type=job_type,
        item_id=item_id,
        input_reference=reference,
        city_id=city_id,
        zone_id=zone_id,
        status="queued",
        lease_until=not_before,
        result_json={},
        options_json=options or {},
    )
    db.add(job)
    db.flush()
    return job


def _prune_product_test_history(
    db: Session,
    *,
    workspace_id: int,
    keep: int = PRODUCT_TEST_HISTORY_LIMIT,
) -> int:
    cutoff_id = db.scalar(
        select(ProductTestJob.id)
        .where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.completed_at.is_not(None),
        )
        .order_by(ProductTestJob.id.desc())
        .offset(max(10, int(keep)))
        .limit(1)
    )
    if cutoff_id is None:
        return 0
    result = db.execute(
        delete(ProductTestJob).where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.completed_at.is_not(None),
            ProductTestJob.id <= cutoff_id,
        )
    )
    return int(result.rowcount or 0)


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
        availability.set("preOrder", str(max(1, item.preorder_days)))
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
    workspace_id = current_workspace_id()
    all_items = list(db.scalars(
        select(ProductTestItem)
        .where(ProductTestItem.workspace_id == workspace_id)
        .order_by(ProductTestItem.updated_at.desc(), ProductTestItem.id.desc())
    ).all())
    now = _now()
    items = [
        item
        for item in all_items
        if item.active and not _is_new_card_item(item) and not _kaspi_submission(item)
    ]
    new_cards = [
        item
        for item in all_items
        if item.active
        and _is_new_card_item(item)
        and (
            not _kaspi_submission(item)
            or (
                _kaspi_submission(item).get("status") == "failed"
                and _kaspi_submission(item).get("stage") == "product_import"
            )
        )
    ]
    submissions = [item for item in all_items if _submission_is_visible(item, now=now)]
    jobs = list(db.scalars(
        select(ProductTestJob)
        .where(ProductTestJob.workspace_id == workspace_id)
        .order_by(ProductTestJob.id.desc())
        .limit(20)
    ).all())
    feed = db.scalar(select(KaspiXmlFeed).where(KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1))
    settings = _settings(db, workspace_id)
    db.commit()
    return {
        "items": [_item_payload(item) for item in items],
        "new_cards": [_item_payload(item) for item in new_cards],
        "submissions": [_item_payload(item) for item in submissions],
        "jobs": [_job_payload(job) for job in jobs],
        "feed": None if feed is None else {"id": feed.id, "source_filename": feed.source_filename, "merchant_id": feed.merchant_id},
        "defaults": {"city_id": settings.city_id, "zone_id": settings.zone_id},
        "settings": _settings_payload(settings),
        "agent": _product_test_agent_status(workspace_id),
    }


def _persist_product_inspection(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    kaspi_id = str(result.get("kaspi_product_id") or "").strip()[:64]
    merchant_sku = str(result.get("merchant_sku") or kaspi_id).strip()[:128]
    name = str(result.get("product_name") or kaspi_id).strip()[:500]
    kaspi_url = str(result.get("product_url") or "").strip()[:4000]
    if not kaspi_id or not merchant_sku or not name or not kaspi_url:
        raise ValueError("Kaspi вернул неполные данные публичной карточки")

    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.workspace_id == job.workspace_id,
            ProductTestItem.merchant_sku == merchant_sku,
        ).with_for_update()
    )
    observed = _money(result.get("page_visible_price_kzt"))
    image_url = normalize_product_image_url(result.get("image_url"))
    if item is None:
        item = ProductTestItem(
            workspace_id=job.workspace_id,
            input_reference=job.input_reference,
            kaspi_product_id=kaspi_id,
            merchant_sku=merchant_sku,
            name=name,
            brand=(str(result.get("brand") or "").strip()[:255] or None),
            image_url=image_url,
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
        item.image_url = image_url
        item.kaspi_url = kaspi_url
        item.observed_price_kzt = observed
        item.offers_json = result.get("offers") if isinstance(result.get("offers"), dict) else {}
        item.active = True

    product = db.scalar(
        select(Product).where(
            Product.workspace_id == job.workspace_id,
            or_(Product.kaspi_product_id == kaspi_id, Product.merchant_sku == merchant_sku),
        ).limit(1)
    )
    if product is not None and item.image_url:
        product.image_url = item.image_url

    # The normalized item already owns the useful offers summary. Keeping the
    # complete scanner response in every historical job duplicated the same
    # JSON and let a manual lab feature grow without a retention boundary.
    job.result_json = {
        "kaspi_product_id": kaspi_id,
        "merchant_sku": merchant_sku,
    }
    job.status = "succeeded"
    job.completed_at = _now()
    job.error_code = None
    job.error_message = None
    db.commit()
    db.refresh(item)
    return {"job": _job_payload(job), "item": _item_payload(item)}


def _finish_job(job: ProductTestJob, result: dict) -> None:
    job.result_json = result
    job.status = "succeeded"
    job.completed_at = _now()
    job.error_code = None
    job.error_message = None


def _supplier_payload(result: dict, *, fallback_url: str) -> dict:
    price = _money(result.get("supplier_price_kzt"))
    url = str(result.get("supplier_url") or fallback_url or "").strip()[:4000]
    if price is None or not url or not result.get("price_confirmed", result.get("validated")):
        raise ValueError("Ozon не подтвердил цену поставщика для новой карточки")
    return {
        "supplier_url": url,
        "supplier_price_kzt": format(price, "f"),
        "supplier_price_source": result.get("supplier_price_source"),
        "supplier_delivery_days": result.get("supplier_delivery_days"),
        "supplier_delivery_text": str(result.get("supplier_delivery_text") or "").strip()[:255] or None,
        "supplier_delivery_date": str(result.get("supplier_delivery_date") or "").strip()[:32] or None,
        "supplier_offer_sku": result.get("supplier_offer_sku"),
        "supplier_seller_name": result.get("supplier_seller_name") or "Ozon",
        "supplier_seller_rating": result.get("supplier_seller_rating"),
        "supplier_seller_reviews": result.get("supplier_seller_reviews"),
        "supplier_offer_count": result.get("supplier_offer_count") or 1,
        "supplier_product_title": str(result.get("supplier_product_title") or "").strip()[:500] or None,
        "supplier_image_url": _external_https_url(result.get("supplier_image_url")),
        "supplier_image_urls": [
            url
            for value in list(result.get("supplier_image_urls") or [])[:6]
            if (url := _external_https_url(value))
        ],
        "supplier_rating": result.get("supplier_rating"),
        "supplier_reviews": result.get("supplier_reviews"),
        "match_status": "OPERATOR_CONFIRMED",
        "match_score": 1.0,
        "match_reasons": ["operator_selected_exact_url"],
        "image_match": {"status": "OPERATOR_CONFIRMED"},
        "manual_override": True,
        "visual_review_required": False,
        "validated": True,
        "validation_source": "manual_exact_product_page",
    }


def _clean_new_card_draft(raw: dict, *, source_url: str) -> dict:
    draft = dict(raw)
    draft["source_url"] = source_url
    draft["sku"] = str(draft.get("sku") or "").strip()[:64]
    draft["title"] = str(draft.get("title") or "").strip()[:1024]
    draft["brand"] = str(draft.get("brand") or "").strip()[:255]
    draft["description"] = str(draft.get("description") or "").strip()[:1024]
    draft["category"] = str(draft.get("category") or "").strip()[:255]
    draft["category_title"] = str(draft.get("category_title") or "").strip()[:500]
    draft["category_hint"] = str(draft.get("category_hint") or "").strip()[:500]
    draft["images"] = [
        url
        for value in list(draft.get("images") or [])[:20]
        if (url := _external_https_url(value))
    ]
    draft["characteristics"] = [
        {
            "name": str(row.get("name") or "").strip()[:255],
            "value": str(row.get("value") or "").strip()[:1200],
        }
        for row in list(draft.get("characteristics") or [])[:180]
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    draft["attributes"] = [dict(row) for row in list(draft.get("attributes") or [])[:300] if isinstance(row, dict)]
    draft["categories"] = [
        {
            "code": str(row.get("code") or "").strip()[:255],
            "title": str(row.get("title") or "").strip()[:500],
        }
        for row in list(draft.get("categories") or [])[:1000]
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]
    supplied_errors = [str(value)[:500] for value in list(draft.get("validation_errors") or [])[:100]]
    draft["validation_errors"] = list(dict.fromkeys([*supplied_errors, *_new_card_draft_errors(draft)]))
    return draft


def _persist_new_card_prepare(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    raw_draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
    source_url = str(raw_draft.get("source_url") or job.options_json.get("supplier_url") or job.input_reference).strip()[:4000]
    draft = _clean_new_card_draft(raw_draft, source_url=source_url)
    supplier_result = result.get("supplier") if isinstance(result.get("supplier"), dict) else {}
    supplier = _supplier_payload(supplier_result, fallback_url=source_url)
    sku = str(draft.get("sku") or "").strip()
    if not sku or not draft.get("title"):
        raise ValueError("Ozon вернул неполный черновик новой карточки")
    existing_product = db.scalar(
        select(Product.id).where(
            Product.workspace_id == job.workspace_id,
            or_(Product.merchant_sku == sku, Product.kaspi_product_id == sku),
        ).limit(1)
    )
    if existing_product is not None:
        raise ValueError("Этот Ozon SKU уже связан с товаром CRM")
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.workspace_id == job.workspace_id,
            ProductTestItem.merchant_sku == sku,
        ).with_for_update()
    )
    if item is not None and not _is_new_card_item(item):
        raise ValueError("SKU уже занят другим заданием Теста товаров")
    if item is not None and _kaspi_submission(item):
        raise ValueError(
            "Этот SKU уже передавался Product Import; используйте сохранённый черновик и не создавайте второй маршрут"
        )
    offers = {"mode": "new_card", "supplier": supplier, "new_card": draft}
    values = {
        "input_reference": source_url,
        "kaspi_product_id": f"NEW-{sku}"[:64],
        "merchant_sku": sku[:128],
        "name": str(draft["title"])[:500],
        "brand": str(draft.get("brand") or "")[:255] or None,
        "image_url": normalize_product_image_url((draft.get("images") or [None])[0]),
        "kaspi_url": "",
        "supplier_url": source_url,
        "offers_json": offers,
        "status": "new_card_ready" if not draft["validation_errors"] else "new_card_draft",
        "last_error": None,
        "active": True,
    }
    settings = _settings(db, job.workspace_id)
    if item is None:
        item = ProductTestItem(
            workspace_id=job.workspace_id,
            observed_price_kzt=None,
            test_price_kzt=None,
            preorder_days=1,
            stock_count=settings.stock_count,
            city_id=settings.city_id,
            zone_id=settings.zone_id,
            **values,
        )
        db.add(item)
    else:
        for key, value in values.items():
            setattr(item, key, value)
    db.flush()
    _refresh_upload_plan(item, settings)
    _finish_job(job, {"item_id": item.id, "sku": sku, "validation_errors": draft["validation_errors"]})
    db.commit()
    db.refresh(item)
    return {"job": _job_payload(job), "item": _item_payload(item)}


def _persist_new_card_mapping(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise ValueError("Черновик новой карточки не найден")
    draft = _new_card_draft(item)
    draft["category"] = str(result.get("category") or job.options_json.get("category") or "").strip()[:255]
    mapped_attributes = [
        dict(row) for row in list(result.get("attributes") or [])[:300] if isinstance(row, dict)
    ]
    draft["attributes"] = _preserve_operator_attribute_values(
        list(draft.get("attributes") or []),
        mapped_attributes,
    )
    categories = list(draft.get("categories") or [])
    match = next((row for row in categories if str(row.get("code") or "") == draft["category"]), None)
    if match is not None:
        draft["category_title"] = str(match.get("title") or "")[:500]
    draft["validation_errors"] = _new_card_draft_errors(draft)
    _set_new_card_draft(item, draft)
    item.status = "new_card_ready" if not draft["validation_errors"] else "new_card_draft"
    item.last_error = None
    _finish_job(job, {"item_id": item.id, "validation_errors": draft["validation_errors"]})
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


def _new_card_confirmation_options(item: ProductTestItem, *, deadline: datetime) -> dict:
    if item.test_price_kzt is None:
        raise ValueError("Не рассчитана стартовая цена новой карточки")
    return {
        "official_sku": item.merchant_sku,
        "model": item.name,
        "initial_price_kzt": int(item.test_price_kzt),
        "stock_count": max(1, int(item.stock_count or 0)),
        "preorder_days": max(1, int(item.preorder_days or 0)),
        "deadline": deadline.isoformat(),
    }


def _new_card_confirmation_deadline(job: ProductTestJob, *, now: datetime) -> datetime:
    raw_deadline = str((job.options_json or {}).get("deadline") or "").strip()
    try:
        deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
    except ValueError:
        deadline = now + timedelta(seconds=PRODUCT_TEST_NEW_CARD_WATCH_SECONDS)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline


def _reschedule_new_card_confirmation(
    db: Session,
    *,
    job: ProductTestJob,
    item: ProductTestItem,
    last_error: str | None = None,
) -> ProductTestJob | None:
    """Keep moderation polling alive across temporary Agent/Kaspi failures."""

    now = _now()
    deadline = _new_card_confirmation_deadline(job, now=now)
    if deadline <= now:
        return None
    # Reuse the single confirmation job instead of adding one row every five
    # minutes for up to seven days of Kaspi moderation.
    job.status = "queued"
    job.agent_id = None
    job.lease_token = None
    job.lease_until = now + timedelta(seconds=PRODUCT_TEST_NEW_CARD_RECHECK_SECONDS)
    job.completed_at = None
    job.error_code = None
    job.error_message = None
    job.options_json = _new_card_confirmation_options(item, deadline=deadline)
    submission = _kaspi_submission(item)
    _set_kaspi_submission(
        item,
        route="new_card",
        stage="moderation",
        status="waiting",
        check_count=int(submission.get("check_count") or 0) + 1,
        next_check_at=job.lease_until.isoformat(),
        last_check_error=(last_error or "")[:1000] or None,
        error=None,
        error_code=None,
        terminal_rejection=False,
    )
    item.status = "new_card_moderation"
    item.last_error = None
    return job


def _persist_new_card_import(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise ValueError("Черновик новой карточки не найден")
    if result.get("result") != "NEW_CARD_ACCEPTED_FOR_MODERATION" or not result.get("detailed_ok"):
        raise ValueError("Kaspi не подтвердил detailed result новой карточки")
    official_sku = str(result.get("sku") or item.merchant_sku).strip()[:128]
    item.merchant_sku = official_sku
    item.status = "new_card_moderation"
    item.last_error = None
    now = _now()
    deadline = now + timedelta(seconds=PRODUCT_TEST_NEW_CARD_WATCH_SECONDS)
    _set_kaspi_submission(
        item,
        route="new_card",
        stage="moderation",
        status="waiting",
        import_code=str(result.get("import_code") or "")[:255] or None,
        official_sku=official_sku,
        queued_at=now.isoformat(),
        completed_at=None,
        detected_at=None,
        hide_after=None,
        deadline=deadline.isoformat(),
        error=None,
        error_code=None,
        terminal_rejection=False,
    )
    _finish_job(job, {
        "item_id": item.id,
        "import_code": result.get("import_code"),
        "detailed_ok": True,
    })
    followup = _queue_job(
        db,
        workspace_id=job.workspace_id,
        job_type="confirm_new_card",
        reference=f"new-card:{item.id}",
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options=_new_card_confirmation_options(item, deadline=deadline),
        not_before=now + timedelta(seconds=PRODUCT_TEST_NEW_CARD_FIRST_CHECK_SECONDS),
    )
    db.commit()
    return {"job": _job_payload(job), "followup": _job_payload(followup), "item": _item_payload(item)}


def _persist_new_card_confirmation(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise ValueError("Новая карточка больше не ожидает подтверждения")
    if result.get("result") != "NEW_CARD_PENDING_MODERATION":
        master_sku = str(result.get("new_card_master_sku") or result.get("master_sku") or "").strip()[:64]
        if not master_sku:
            raise ValueError("Kaspi не вернул masterSku новой карточки")
        item.kaspi_product_id = master_sku
        item.kaspi_url = f"https://kaspi.kz/shop/p/{master_sku}/"
        return _enroll_created_product(db, job=job, result=result)

    _finish_job(job, {"item_id": item.id, "result": "NEW_CARD_PENDING_MODERATION"})
    now = _now()
    deadline = _new_card_confirmation_deadline(job, now=now)
    if deadline <= now:
        item.status = "new_card_error"
        item.last_error = "Kaspi не назначил masterSku новой карточке за 7 дней"
        _set_kaspi_submission(
            item,
            status="failed",
            stage="moderation_timeout",
            completed_at=now.isoformat(),
            error=item.last_error,
            error_code="NEW_CARD_MODERATION_TIMEOUT",
        )
        db.commit()
        return {"job": _job_payload(job), "item": _item_payload(item)}
    followup = _reschedule_new_card_confirmation(db, job=job, item=item)
    if followup is None:  # Guarded by the deadline check above.
        raise ValueError("Не удалось запланировать следующую проверку новой карточки")
    db.commit()
    return {"job": _job_payload(job), "followup": _job_payload(followup), "item": _item_payload(item)}


def _persist_discovery(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    persisted: list[ProductTestItem] = []
    settings = _settings(db, job.workspace_id)
    for row in list(result.get("rows") or [])[:100]:
        if not isinstance(row, dict):
            continue
        kaspi_id = str(row.get("kaspi_product_id") or "").strip()[:64]
        name = str(row.get("product_name") or "").strip()[:500]
        kaspi_url = str(row.get("product_url") or "").strip()[:4000]
        if not kaspi_id or not name or not kaspi_url:
            continue
        existing_product = db.scalar(
            select(Product.id).where(
                Product.workspace_id == job.workspace_id,
                Product.kaspi_product_id == kaspi_id,
            ).limit(1)
        )
        if existing_product is not None:
            continue
        item = db.scalar(
            select(ProductTestItem).where(
                ProductTestItem.workspace_id == job.workspace_id,
                ProductTestItem.kaspi_product_id == kaspi_id,
            ).with_for_update()
        )
        # A card already handed to Kaspi belongs to the publication-control
        # list.  A later discovery must never overwrite that transaction.
        if item is not None and _kaspi_submission(item):
            continue
        supplier_price = _money(row.get("supplier_price_kzt"))
        supplier_url = str(row.get("supplier_url") or "").strip()[:4000] or None
        match_status = str(row.get("match_status") or "NO_RESULT")
        supplier = {
            "supplier_url": supplier_url,
            "supplier_price_kzt": None if supplier_price is None else format(supplier_price, "f"),
            "supplier_price_source": row.get("supplier_price_source"),
            "supplier_delivery_days": row.get("supplier_delivery_days"),
            "supplier_delivery_text": str(row.get("supplier_delivery_text") or "").strip()[:255] or None,
            "supplier_delivery_date": str(row.get("supplier_delivery_date") or "").strip()[:32] or None,
            "supplier_offer_sku": row.get("supplier_offer_sku"),
            "supplier_seller_name": row.get("supplier_seller_name"),
            "supplier_seller_rating": row.get("supplier_seller_rating"),
            "supplier_seller_reviews": row.get("supplier_seller_reviews"),
            "supplier_product_title": str(row.get("supplier_product_title") or "").strip()[:500] or None,
            "supplier_image_url": _external_https_url(row.get("supplier_image_url")),
            "supplier_image_urls": [
                url
                for value in list(row.get("supplier_image_urls") or [])[:6]
                if (url := _external_https_url(value))
            ],
            "supplier_offer_count": row.get("supplier_offer_count"),
            "supplier_rating": row.get("supplier_rating"),
            "supplier_reviews": row.get("supplier_reviews"),
            "match_status": match_status,
            "match_score": row.get("match_score"),
            "match_reasons": list(row.get("match_reasons") or [])[:12],
            "image_match": row.get("image_match") if isinstance(row.get("image_match"), dict) else {},
            "queries_tested": row.get("queries_tested"),
            "strict_candidates_checked": row.get("strict_candidates_checked"),
            "priced_strict_candidates": row.get("priced_strict_candidates"),
            "total_supplier_offers_checked": row.get("total_supplier_offers_checked"),
            "selection_reason": row.get("selection_reason"),
            "visual_review_required": True,
            "validated": bool(
                supplier_url
                and supplier_price is not None
                and match_status == "CONFIRMED"
                and _external_https_url(row.get("supplier_image_url"))
            ),
            "validation_source": (
                "strict_multimodal_card_price_fallback"
                if str(row.get("supplier_price_source") or "").startswith("search_card.")
                else "strict_multimodal_product_page_price"
                if str(row.get("supplier_price_source") or "").startswith("product_page.")
                else "strict_multimodal_lowest_offer"
            ),
        }
        offers = row.get("offers") if isinstance(row.get("offers"), dict) else {}
        offers = {**offers, "supplier": supplier}
        values = {
            "input_reference": job.input_reference,
            "name": name,
            "brand": str(row.get("brand") or "").strip()[:255] or None,
            "image_url": normalize_product_image_url(row.get("image_url")),
            "kaspi_url": kaspi_url,
            "supplier_url": supplier_url,
            "observed_price_kzt": _money(row.get("page_visible_price_kzt")),
            "offers_json": offers,
            "status": "ready_to_add" if supplier["validated"] else "needs_supplier_link",
            "last_error": None,
            "active": True,
        }
        if item is None:
            item = ProductTestItem(
                workspace_id=job.workspace_id,
                kaspi_product_id=kaspi_id,
                merchant_sku=kaspi_id,
                test_price_kzt=None,
                city_id=job.city_id,
                zone_id=job.zone_id,
                stock_count=5,
                preorder_days=1,
                **values,
            )
            db.add(item)
        else:
            for key, value in values.items():
                setattr(item, key, value)
        _refresh_upload_plan(item, settings)
        persisted.append(item)
    _finish_job(job, {
        "mode": result.get("mode") or (job.options_json or {}).get("mode") or "full",
        "persisted_count": len(persisted),
        "scanned": result.get("scanned"),
        "eligible_new": result.get("eligible_new"),
        "matched_products_checked": result.get("matched_products_checked"),
        "confirmed_pairs": result.get("confirmed_pairs"),
        "manual_review_pairs": result.get("manual_review_pairs"),
        "minimum_reviews": result.get("minimum_reviews"),
        "maximum_sellers": result.get("maximum_sellers"),
        "seller_counts_checked": result.get("seller_counts_checked"),
        "excluded_below_min_reviews": result.get("excluded_below_min_reviews"),
        "excluded_too_many_sellers": result.get("excluded_too_many_sellers"),
        "excluded_unknown_sellers": result.get("excluded_unknown_sellers"),
        "lookup_error_count": len(list(result.get("lookup_errors") or [])),
    })
    db.commit()
    return {"job": _job_payload(job), "items": [_item_payload(item) for item in persisted]}


def _persist_supplier_validation(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        ).with_for_update()
    )
    if item is None:
        raise ValueError("Тестовый товар больше не существует")
    price = _money(result.get("supplier_price_kzt"))
    url = str(result.get("supplier_url") or item.supplier_url or "").strip()[:4000]
    price_confirmed = bool(result.get("price_confirmed", result.get("validated")))
    supplier_image = _external_https_url(result.get("supplier_image_url"))
    if price is None or not url or not price_confirmed:
        raise ValueError("Ozon не подтвердил цену поставщика")
    item.supplier_url = url
    details = dict(item.offers_json or {})
    manual_override = bool(result.get("manual_override"))
    details["supplier"] = {
        "supplier_url": url,
        "supplier_price_kzt": format(price, "f"),
        "supplier_price_source": result.get("supplier_price_source"),
        "supplier_delivery_days": result.get("supplier_delivery_days"),
        "supplier_delivery_text": str(result.get("supplier_delivery_text") or "").strip()[:255] or None,
        "supplier_delivery_date": str(result.get("supplier_delivery_date") or "").strip()[:32] or None,
        "supplier_offer_sku": result.get("supplier_offer_sku"),
        "supplier_seller_name": result.get("supplier_seller_name"),
        "supplier_seller_rating": result.get("supplier_seller_rating"),
        "supplier_seller_reviews": result.get("supplier_seller_reviews"),
        "supplier_offer_count": result.get("supplier_offer_count"),
        "supplier_product_title": str(result.get("supplier_product_title") or "").strip()[:500] or None,
        "supplier_image_url": supplier_image,
        "supplier_image_urls": [
            url
            for value in list(result.get("supplier_image_urls") or [])[:6]
            if (url := _external_https_url(value))
        ],
        "supplier_rating": result.get("supplier_rating"),
        "supplier_reviews": result.get("supplier_reviews"),
        "match_status": result.get("match_status") or "MANUAL_REVIEW",
        "match_score": result.get("match_score"),
        "match_reasons": list(result.get("match_reasons") or [])[:12],
        "image_match": result.get("image_match") if isinstance(result.get("image_match"), dict) else {},
        "manual_override": manual_override,
        "visual_review_required": bool(result.get("visual_review_required", not manual_override)),
        "validated": bool(result.get("validated")),
        "validation_source": (
            "manual_exact_product_page"
            if manual_override
            else "manual_url_product_page_price"
        ),
    }
    item.offers_json = details
    item.status = "ready_to_add" if details["supplier"]["validated"] else "needs_supplier_link"
    item.last_error = (
        None
        if details["supplier"]["validated"]
        else "Цена Ozon подтверждена, но фото карточки не получено. Проверьте или замените ссылку."
    )
    _refresh_upload_plan(item, _settings(db, job.workspace_id))
    _finish_job(job, {"item_id": item.id, "supplier_price_kzt": format(price, "f")})
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


def _enroll_created_product(db: Session, *, job: ProductTestJob, result: dict) -> dict:
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        ).with_for_update()
    )
    if item is None:
        raise ValueError("Тестовый товар больше не существует")
    state = result.get("after") or result.get("before") or {}
    merchant_sku = str(result.get("merchant_sku") or state.get("sku") or "").strip()[:128]
    actual_price = _money(state.get("price_kzt"))
    if not state.get("found") or not merchant_sku or actual_price is None:
        raise ValueError("Kaspi ещё не подтвердил созданный оффер и его цену")
    actual_preorder_days = int(state.get("preorder_days") or 0)
    expected_preorder_days = max(1, int(item.preorder_days or 0))
    if actual_preorder_days != expected_preorder_days:
        raise ValueError(
            f"Kaspi ещё не подтвердил предзаказ {expected_preorder_days} дн. созданного оффера"
        )

    product = db.scalar(
        select(Product).where(
            Product.workspace_id == job.workspace_id,
            or_(Product.kaspi_product_id == item.kaspi_product_id, Product.merchant_sku == merchant_sku),
        ).with_for_update()
    )
    if product is None:
        product = Product(
            workspace_id=job.workspace_id,
            kaspi_product_id=item.kaspi_product_id,
            merchant_sku=merchant_sku,
            name=item.name,
            brand=item.brand,
            image_url=item.image_url,
            status=ProductStatus.ACTIVE.value,
            sale_enabled=True,
        )
        db.add(product)
        db.flush()
    else:
        product.merchant_sku = merchant_sku
        product.name = item.name
        product.brand = item.brand
        product.image_url = item.image_url or product.image_url
        product.status = ProductStatus.ACTIVE.value
        product.sale_enabled = True

    supplier_info = (item.offers_json or {}).get("supplier") or {}
    supplier_cost = _money(supplier_info.get("supplier_price_kzt"))
    if supplier_cost is None or not item.supplier_url:
        raise ValueError("Подтверждённая цена поставщика потеряна")
    supplier = db.scalar(
        select(Supplier).where(Supplier.workspace_id == job.workspace_id, Supplier.code == "ozon").with_for_update()
    )
    if supplier is None:
        supplier = Supplier(workspace_id=job.workspace_id, code="ozon", name="Ozon", is_active=True)
        db.add(supplier)
        db.flush()
    external_id = str(supplier_info.get("supplier_offer_sku") or item.supplier_url).strip()[:255]
    supplier_product = db.scalar(
        select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier.id,
            SupplierProduct.external_id == external_id,
        ).with_for_update()
    )
    now = _now()
    delivery_days = int(supplier_info.get("supplier_delivery_days") or 0)
    if supplier_product is None:
        supplier_product = SupplierProduct(
            workspace_id=job.workspace_id,
            supplier_id=supplier.id,
            external_id=external_id,
            title=item.name,
            url=item.supplier_url,
            current_price=supplier_cost,
            delivery_days=delivery_days,
            in_stock=True,
            last_checked_at=now,
        )
        db.add(supplier_product)
        db.flush()
    else:
        supplier_product.title = item.name
        supplier_product.url = item.supplier_url
        supplier_product.current_price = supplier_cost
        supplier_product.delivery_days = delivery_days
        supplier_product.in_stock = True
        supplier_product.last_checked_at = now

    binding = db.scalar(
        select(ProductBinding).where(
            ProductBinding.product_id == product.id,
            ProductBinding.supplier_product_id == supplier_product.id,
        ).with_for_update()
    )
    if binding is None:
        binding = ProductBinding(
            workspace_id=job.workspace_id,
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            status="active",
            decision_source="automatic",
            is_primary=True,
            confidence_score=100,
            confirmed_at=now,
            last_validated_at=now,
        )
        db.add(binding)
        db.flush()
    else:
        binding.status = "active"
        binding.is_primary = True
        binding.last_validated_at = now

    target = db.scalar(select(MonitorTarget).where(MonitorTarget.product_binding_id == binding.id).with_for_update())
    if target is None:
        target = MonitorTarget(
            workspace_id=job.workspace_id,
            product_binding_id=binding.id,
            status=MonitorStatus.ACTIVE.value,
            interval_seconds=600,
            next_check_at=now,
        )
        db.add(target)
        db.flush()
    else:
        target.status = MonitorStatus.ACTIVE.value
        target.next_check_at = now

    fingerprint = offer_fingerprint(
        supplier_product_id=supplier_product.id,
        price=supplier_cost,
        available=True,
        stock=None,
        delivery_days=delivery_days,
        seller=supplier_info.get("supplier_seller_name"),
        adapter_schema_version="ozon-http-session-v1",
        currency="KZT",
    )
    offer_state = db.scalar(
        select(SupplierOfferState).where(SupplierOfferState.supplier_product_id == supplier_product.id).with_for_update()
    )
    offer_changed = offer_state is None or offer_state.fingerprint != fingerprint
    if offer_state is None:
        offer_state = SupplierOfferState(
            workspace_id=job.workspace_id,
            supplier_product_id=supplier_product.id,
            price=supplier_cost,
            currency="KZT",
            available=True,
            delivery_days=delivery_days,
            seller=supplier_info.get("supplier_seller_name"),
            fingerprint=fingerprint,
            adapter_schema_version="ozon-http-session-v1",
            observed_at=now,
            last_checked_at=now,
        )
        db.add(offer_state)
    elif offer_changed:
        offer_state.price = supplier_cost
        offer_state.old_price = None
        offer_state.currency = "KZT"
        offer_state.available = True
        offer_state.stock = None
        offer_state.delivery_days = delivery_days
        offer_state.seller = supplier_info.get("supplier_seller_name")
        offer_state.fingerprint = fingerprint
        offer_state.adapter_schema_version = "ozon-http-session-v1"
        offer_state.observed_at = now
        offer_state.last_checked_at = now
        offer_state.version += 1
    else:
        offer_state.last_checked_at = now

    if offer_changed:
        db.add(SupplierOfferObservation(
            workspace_id=job.workspace_id,
            supplier_product_id=supplier_product.id,
            price=supplier_cost,
            currency="KZT",
            available=True,
            delivery_days=delivery_days,
            seller=supplier_info.get("supplier_seller_name"),
            fingerprint=fingerprint,
            adapter_schema_version="ozon-http-session-v1",
            raw_metadata=json.dumps({"source": "product_test_validated", "execution_surface": "local_http_agent"}),
            observed_at=now,
        ))

    settings = _settings(db, job.workspace_id)
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.workspace_id == job.workspace_id,
            FastDumpingPolicy.product_id == product.id,
        ).with_for_update()
    )
    policy_values = {
        "enabled": True,
        "minimum_profit_kzt": settings.minimum_profit_kzt,
        "undercut_step_kzt": settings.undercut_step_kzt,
        "allow_price_raise": settings.allow_price_raise,
        "max_undercut_gap_percent": settings.max_undercut_gap_percent,
        "scan_interval_seconds": settings.scan_interval_seconds,
        "delivery_price_premium_kzt": settings.delivery_price_premium_kzt,
        "delivery_advantage_days": settings.delivery_advantage_days,
        "preorder_target_position": settings.preorder_target_position,
        "city_id": settings.city_id,
        "zone_id": settings.zone_id,
    }
    if policy is None:
        policy = FastDumpingPolicy(workspace_id=job.workspace_id, product_id=product.id, **policy_values)
        db.add(policy)
        db.flush()
    else:
        for key, value in policy_values.items():
            setattr(policy, key, value)
    ensure_state(db, policy=policy, workspace_id=job.workspace_id)
    queue_scan(db, policy=policy, workspace_id=job.workspace_id, reason="product_test_auto_enroll")
    queue_browser_target_now(db, target_id=target.id, supplier_code="ozon")

    item.merchant_sku = merchant_sku
    item.product_id = product.id
    item.fast_dumping_policy_id = policy.id
    item.test_price_kzt = actual_price
    item.status = "enrolled_fast_dumping"
    item.active = False
    item.added_at = now
    item.last_error = None
    _set_kaspi_submission(
        item,
        status="succeeded",
        completed_at=now.isoformat(),
        detected_at=now.isoformat(),
        hide_after=(now + timedelta(seconds=PRODUCT_TEST_SUCCESS_VISIBLE_SECONDS)).isoformat(),
        merchant_sku=merchant_sku,
        actual_price_kzt=format(actual_price, "f"),
        product_id=product.id,
        fast_dumping_policy_id=policy.id,
        error=None,
        error_code=None,
        terminal_rejection=False,
    )
    _finish_job(job, {"product_id": product.id, "policy_id": policy.id, "merchant_sku": merchant_sku})
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item), "product_id": product.id, "fast_dumping_policy_id": policy.id}


@router.post("/inspect")
def inspect_product(payload: ProductTestInspectRequest, db: Session = Depends(get_db)) -> dict:
    """Queue one explicit card read for the local Windows Fast Agent."""

    reference = payload.reference.strip()
    workspace_id = current_workspace_id()
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="inspect",
        reference=reference,
        city_id=payload.city_id.strip(),
        zone_id=payload.zone_id.strip(),
    )
    _prune_product_test_history(db, workspace_id=job.workspace_id)
    db.commit()
    db.refresh(job)
    return {"job": _job_payload(job), "queued": True}


@router.post("/discover")
def discover_product_candidates(payload: ProductDiscoveryRequest, db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    settings = _settings(db, workspace_id)
    existing_ids = list(
        db.scalars(
            select(Product.kaspi_product_id).where(Product.workspace_id == workspace_id)
        ).all()
    )
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="discover_popular" if payload.mode == "popular" else "discover",
        reference=payload.query.strip(),
        city_id=settings.city_id,
        zone_id=settings.zone_id,
        options={
            "mode": payload.mode,
            "target_new": payload.target_new or settings.target_new,
            "max_kaspi_scan": settings.max_kaspi_scan,
            "max_ozon_queries": settings.max_ozon_queries,
            "minimum_reviews": payload.minimum_reviews,
            "maximum_sellers": payload.maximum_sellers,
            # Visual verification is part of the operator approval contract and
            # cannot be disabled for new discovery jobs.
            "image_verify": True,
            "existing_kaspi_ids": existing_ids,
        },
    )
    # Starting a new scan replaces the visual-candidate batch immediately.
    # Items already submitted to Kaspi are preserved in their own control list.
    for item in db.scalars(
        select(ProductTestItem).where(
            ProductTestItem.workspace_id == workspace_id,
            ProductTestItem.active.is_(True),
        ).with_for_update()
    ):
        if not _kaspi_submission(item) and not _is_new_card_item(item):
            item.active = False
    _prune_product_test_history(db, workspace_id=workspace_id)
    db.commit()
    db.refresh(job)
    return {"job": _job_payload(job), "queued": True}


@router.post("/new-cards/prepare")
def prepare_product_test_new_card(
    payload: ProductTestNewCardRequest,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    settings = _settings(db, workspace_id)
    supplier_url = payload.supplier_url.strip()
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="prepare_new_card",
        reference=supplier_url,
        city_id=settings.city_id,
        zone_id=settings.zone_id,
        options={"supplier_url": supplier_url},
    )
    _prune_product_test_history(db, workspace_id=workspace_id)
    db.commit()
    return {"job": _job_payload(job), "queued": True}


@router.patch("/new-cards/{item_id}")
def update_product_test_new_card(
    item_id: int,
    payload: ProductTestNewCardUpdate,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == item_id,
            ProductTestItem.workspace_id == workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise HTTPException(status_code=404, detail="Черновик новой карточки не найден")
    if item.status in {"new_card_importing", "new_card_moderation", "enrolled_fast_dumping"}:
        raise HTTPException(status_code=409, detail="Карточка уже передана Kaspi и временно заблокирована для правок")
    active_mapping = db.scalar(
        select(ProductTestJob.id).where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.item_id == item.id,
            ProductTestJob.job_type == "map_new_card_category",
            ProductTestJob.status.in_(("queued", "leased")),
        ).limit(1)
    )
    if active_mapping is not None:
        raise HTTPException(
            status_code=409,
            detail="Product Test Agent ещё загружает поля категории. Дождитесь завершения — кнопка включится автоматически.",
        )
    draft = _new_card_draft(item)
    changes = payload.model_dump(exclude_unset=True)
    requested_sku = str(changes.get("sku") or draft.get("sku") or "").strip()[:128]
    previous_submission = _kaspi_submission(item)
    if (
        requested_sku
        and previous_submission.get("route") == "new_card"
        and requested_sku != item.merchant_sku
    ):
        raise HTTPException(
            status_code=409,
            detail="После первой Product Import отправки SKU менять нельзя; исправьте остальные поля и повторите тот же SKU",
        )
    if requested_sku and requested_sku != item.merchant_sku:
        occupied_item = db.scalar(
            select(ProductTestItem.id).where(
                ProductTestItem.workspace_id == workspace_id,
                ProductTestItem.merchant_sku == requested_sku,
                ProductTestItem.id != item.id,
            ).limit(1)
        )
        occupied_product = db.scalar(
            select(Product.id).where(
                Product.workspace_id == workspace_id,
                or_(
                    Product.merchant_sku == requested_sku,
                    Product.kaspi_product_id == requested_sku,
                ),
            ).limit(1)
        )
        if occupied_item is not None or occupied_product is not None:
            raise HTTPException(status_code=409, detail="Этот SKU уже занят в CRM или другом задании")
    for key, value in changes.items():
        if key == "images":
            value = [
                url
                for raw in list(value or [])[:20]
                if (url := _external_https_url(raw))
            ]
        elif key == "attributes":
            value = _operator_attribute_updates(
                list(draft.get("attributes") or []),
                [dict(row) for row in list(value or [])[:300] if isinstance(row, dict)],
            )
        elif key == "weight":
            value = None if value is None else format(value, "f")
        elif isinstance(value, str):
            value = value.strip()
        draft[key] = value
    draft["validation_errors"] = _new_card_draft_errors(draft)
    _set_new_card_draft(item, draft)
    item.merchant_sku = str(draft.get("sku") or item.merchant_sku).strip()[:128]
    item.name = str(draft.get("title") or item.name).strip()[:500]
    item.brand = str(draft.get("brand") or "").strip()[:255] or None
    item.image_url = normalize_product_image_url((draft.get("images") or [None])[0])
    item.status = "new_card_ready" if not draft["validation_errors"] else "new_card_draft"
    item.last_error = None
    db.commit()
    db.refresh(item)
    return _item_payload(item)


@router.post("/new-cards/{item_id}/map-category")
def map_product_test_new_card_category(
    item_id: int,
    payload: ProductTestNewCardCategoryRequest,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == item_id,
            ProductTestItem.workspace_id == workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise HTTPException(status_code=404, detail="Черновик новой карточки не найден")
    if item.status in {"new_card_importing", "new_card_moderation", "enrolled_fast_dumping"}:
        raise HTTPException(status_code=409, detail="Карточка уже передана Kaspi")
    active_mapping = db.scalar(
        select(ProductTestJob.id).where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.item_id == item.id,
            ProductTestJob.job_type == "map_new_card_category",
            ProductTestJob.status.in_(("queued", "leased")),
        ).limit(1)
    )
    if active_mapping is not None:
        raise HTTPException(
            status_code=409,
            detail="Product Test Agent ещё загружает поля категории. Дождитесь завершения и повторите создание.",
        )
    draft = _new_card_draft(item)
    category = payload.category.strip()
    item.status = "new_card_mapping"
    item.last_error = None
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="map_new_card_category",
        reference=f"new-card:{item.id}",
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options={
            "category": category,
            "characteristics": list(draft.get("characteristics") or [])[:180],
        },
    )
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


@router.post("/new-cards/{item_id}/create")
def create_product_test_new_card(item_id: int, db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == item_id,
            ProductTestItem.workspace_id == workspace_id,
        ).with_for_update()
    )
    if item is None or not _is_new_card_item(item):
        raise HTTPException(status_code=404, detail="Черновик новой карточки не найден")
    if item.status in {"new_card_importing", "new_card_moderation", "enrolled_fast_dumping"}:
        raise HTTPException(status_code=409, detail="Карточка уже передана Kaspi")
    active_mapping = db.scalar(
        select(ProductTestJob.id).where(
            ProductTestJob.workspace_id == workspace_id,
            ProductTestJob.item_id == item.id,
            ProductTestJob.job_type == "map_new_card_category",
            ProductTestJob.status.in_(("queued", "leased")),
        ).limit(1)
    )
    if active_mapping is not None:
        raise HTTPException(
            status_code=409,
            detail="Product Test Agent ещё загружает поля категории. Дождитесь завершения и повторите создание.",
        )
    previous = _kaspi_submission(item)
    if previous.get("route") == "new_card" and previous.get("stage") != "product_import":
        raise HTTPException(
            status_code=409,
            detail="Product Import уже был принят Kaspi; повторно отправлять SKU нельзя — проверьте карточку в кабинете",
        )
    draft = _new_card_draft(item)
    errors = _new_card_draft_errors(draft)
    if errors:
        draft["validation_errors"] = errors
        _set_new_card_draft(item, draft)
        db.commit()
        raise HTTPException(status_code=409, detail="; ".join(errors[:8]))
    supplier = (item.offers_json or {}).get("supplier") or {}
    if not supplier.get("validated") or _money(supplier.get("supplier_price_kzt")) is None:
        raise HTTPException(status_code=409, detail="Ozon не подтвердил цену новой карточки")
    settings = _settings(db, workspace_id)
    pricing = _refresh_upload_plan(item, settings)
    if pricing is None or item.test_price_kzt is None:
        raise HTTPException(status_code=409, detail="Не рассчитана стартовая цена Kaspi")
    item.status = "new_card_importing"
    item.last_error = None
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="create_new_card",
        reference=f"new-card:{item.id}",
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options={"draft": _new_card_job_draft(item)},
    )
    now = _now()
    _set_kaspi_submission(
        item,
        route="new_card",
        stage="product_import",
        status="waiting",
        queued_at=now.isoformat(),
        completed_at=None,
        detected_at=None,
        hide_after=None,
        job_id=job.id,
        attempt=int(previous.get("attempt") or 0) + 1,
        official_sku=item.merchant_sku,
        initial_price_kzt=format(item.test_price_kzt, "f"),
        preorder_days=max(1, int(item.preorder_days or 0)),
        error=None,
        error_code=None,
        terminal_rejection=False,
    )
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


@router.patch("/settings")
def update_product_test_settings(payload: ProductTestSettingsUpdate, db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    settings = _settings(db, workspace_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(settings, key, value.strip() if isinstance(value, str) else value)
    if settings.target_new > settings.max_kaspi_scan:
        raise HTTPException(status_code=422, detail="Лимит сканирования должен быть не меньше числа новых товаров")
    for item in db.scalars(
        select(ProductTestItem).where(
            ProductTestItem.workspace_id == workspace_id,
            ProductTestItem.active.is_(True),
        ).with_for_update()
    ):
        if not _kaspi_submission(item):
            _refresh_upload_plan(item, settings)
    db.commit()
    db.refresh(settings)
    return _settings_payload(settings)


@router.patch("/items/{item_id}")
def update_product_test_item(item_id: int, payload: ProductTestUpdate, db: Session = Depends(get_db)) -> dict:
    item = db.get(ProductTestItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Тестовый товар не найден")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(item, key, value)
        if key == "supplier_url":
            item.status = "needs_supplier_validation"
            item.last_error = None
    db.commit()
    db.refresh(item)
    return _item_payload(item)


@router.post("/items/{item_id}/validate-supplier")
def validate_product_supplier(item_id: int, payload: SupplierUrlRequest, db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    item = db.scalar(select(ProductTestItem).where(ProductTestItem.id == item_id, ProductTestItem.workspace_id == workspace_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Тестовый товар не найден")
    item.supplier_url = payload.supplier_url.strip()
    item.status = "validating_supplier"
    item.last_error = None
    details = item.offers_json if isinstance(item.offers_json, dict) else {}
    kaspi_offer = details.get("kaspi") if isinstance(details.get("kaspi"), dict) else {}
    kaspi_images = [
        url
        for value in list(kaspi_offer.get("image_urls") or [])[:6]
        if (url := _external_https_url(value))
    ]
    if item.image_url and item.image_url not in kaspi_images:
        kaspi_images.insert(0, item.image_url)
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="validate_supplier",
        reference=f"item:{item.id}",
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options={
            "supplier_url": item.supplier_url,
            "product": {
                "title": item.name,
                "brand": item.brand,
                "image_url": item.image_url,
                "image_urls": kaspi_images,
            },
        },
    )
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


@router.post("/items/{item_id}/add")
def add_product_to_kaspi(item_id: int, db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    item = db.scalar(
        select(ProductTestItem).where(ProductTestItem.id == item_id, ProductTestItem.workspace_id == workspace_id).with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Тестовый товар не найден")
    supplier = (item.offers_json or {}).get("supplier") or {}
    supplier_cost = _money(supplier.get("supplier_price_kzt"))
    validated_url = str(supplier.get("supplier_url") or "").strip()
    previous_submission = _kaspi_submission(item)
    retrying_failed_submission = item.status == "error" and previous_submission.get("status") == "failed"
    if (
        (item.status != "ready_to_add" and not retrying_failed_submission)
        or not item.supplier_url
        or item.supplier_url.strip() != validated_url
        or not supplier.get("validated")
        or supplier_cost is None
    ):
        raise HTTPException(status_code=409, detail="Сначала подтвердите ссылку и цену поставщика Ozon")
    settings = _settings(db, workspace_id)
    pricing = _refresh_upload_plan(item, settings)
    if pricing is None or item.test_price_kzt is None:
        raise HTTPException(status_code=409, detail="Не удалось рассчитать стартовые параметры Kaspi")
    preorder_days = max(1, int(item.preorder_days or 0))
    item.status = "adding_to_kaspi"
    item.last_error = None
    job = _queue_job(
        db,
        workspace_id=workspace_id,
        job_type="create_offer",
        reference=f"item:{item.id}",
        item_id=item.id,
        city_id=settings.city_id,
        zone_id=settings.zone_id,
        options={
            "master_sku": item.kaspi_product_id,
            "model": item.name,
            "initial_price_kzt": int(item.test_price_kzt),
            "stock_count": settings.stock_count,
            "preorder_days": preorder_days,
        },
    )
    queued_at = _now()
    _set_kaspi_submission(
        item,
        status="waiting",
        queued_at=queued_at.isoformat(),
        completed_at=None,
        detected_at=None,
        hide_after=None,
        job_id=job.id,
        attempt=int(previous_submission.get("attempt") or 0) + 1,
        initial_price_kzt=format(item.test_price_kzt, "f"),
        error=None,
        error_code=None,
        terminal_rejection=False,
    )
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


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
def claim_product_test_job(
    payload: ProductTestAgentIdentity,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    """Lease one Product Test job only to the dedicated local agent."""

    if getattr(payload, "agent_kind", None) != PRODUCT_TEST_AGENT_KIND:
        return {
            "job": None,
            "retry_after_seconds": 300,
            "agent_required": PRODUCT_TEST_AGENT_KIND,
        }

    try:
        _validate_workspace_merchant(
            db,
            workspace_id=payload.workspace_id,
            merchant_uid=payload.merchant_uid,
        )
        now = _now()
        with workspace_context(payload.workspace_id):
            _touch_product_test_agent(payload)
            db.execute(
                update(ProductTestJob)
                .where(
                    ProductTestJob.workspace_id == payload.workspace_id,
                    ProductTestJob.status == "leased",
                    ProductTestJob.lease_until.is_not(None),
                    ProductTestJob.lease_until <= now,
                )
                .values(
                    status="queued",
                    agent_id=None,
                    lease_token=None,
                    lease_until=None,
                )
            )
            job = db.scalar(
                select(ProductTestJob)
                .where(
                    ProductTestJob.workspace_id == payload.workspace_id,
                    ProductTestJob.status == "queued",
                    or_(
                        ProductTestJob.lease_until.is_(None),
                        ProductTestJob.lease_until <= now,
                    ),
                )
                .order_by(ProductTestJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                db.commit()
                return {"job": None, "retry_after_seconds": 5}
            job.status = "leased"
            job.agent_id = payload.agent_id
            job.lease_token = uuid4().hex
            lease_seconds = (
                1800
                if job.job_type in {
                    "create_offer", "create_new_card", "confirm_new_card", "discover", "discover_popular"
                }
                else PRODUCT_TEST_LEASE_SECONDS
            )
            job.lease_until = now + timedelta(seconds=lease_seconds)
            result = {
                "id": job.id,
                "job_type": job.job_type,
                "item_id": job.item_id,
                "reference": job.input_reference,
                "city_id": job.city_id,
                "zone_id": job.zone_id,
                "options": job.options_json or {},
                "lease_token": job.lease_token,
            }
            db.commit()
            return {"job": result, "retry_after_seconds": 0}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@agent_router.post("/heartbeat")
def heartbeat_product_test_agent(
    payload: ProductTestAgentHeartbeat,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    if payload.agent_kind != PRODUCT_TEST_AGENT_KIND:
        raise HTTPException(status_code=409, detail="Требуется отдельный Product Test Agent")
    try:
        _validate_workspace_merchant(
            db,
            workspace_id=payload.workspace_id,
            merchant_uid=payload.merchant_uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _touch_product_test_agent(payload, status=payload.status)


@agent_router.get("/agents/status")
def read_product_test_agent_status() -> dict:
    return _product_test_agent_status(current_workspace_id())


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
        job.lease_token = None
        job.lease_until = None
        if payload.status == "failed":
            job.status = "failed"
            if job.item_id is not None:
                item = db.scalar(
                    select(ProductTestItem).where(
                        ProductTestItem.id == job.item_id,
                        ProductTestItem.workspace_id == payload.workspace_id,
                    ).with_for_update()
                )
                if item is not None:
                    if _is_new_card_item(item) and job.job_type == "confirm_new_card":
                        followup = _reschedule_new_card_confirmation(
                            db,
                            job=job,
                            item=item,
                            last_error=payload.error_message or payload.error_code,
                        )
                        if followup is not None:
                            db.commit()
                            return {
                                **_job_payload(job),
                                "retry_job": _job_payload(followup),
                            }
                    item.status = "new_card_error" if _is_new_card_item(item) else "error"
                    item.last_error = (payload.error_message or payload.error_code or "Локальный Agent завершил задание с ошибкой")[:4000]
                    if job.job_type == "create_offer":
                        hide_after = _category_rejection_hide_after(
                            error_code=payload.error_code,
                            error_message=payload.error_message,
                        )
                        _set_kaspi_submission(
                            item,
                            status="failed",
                            completed_at=_now().isoformat(),
                            hide_after=hide_after,
                            terminal_rejection=bool(hide_after),
                            error=item.last_error,
                            error_code=payload.error_code,
                        )
                    elif _is_new_card_item(item) and job.job_type in {"create_new_card", "confirm_new_card"}:
                        _set_kaspi_submission(
                            item,
                            route="new_card",
                            stage="product_import" if job.job_type == "create_new_card" else "moderation",
                            status="failed",
                            completed_at=_now().isoformat(),
                            hide_after=None,
                            terminal_rejection=False,
                            error=item.last_error,
                            error_code=payload.error_code,
                        )
            db.commit()
            return _job_payload(job)

        try:
            if job.job_type in {"discover", "discover_popular"}:
                return _persist_discovery(db, job=job, result=payload.result)
            if job.job_type == "validate_supplier":
                return _persist_supplier_validation(db, job=job, result=payload.result)
            if job.job_type == "create_offer":
                return _enroll_created_product(db, job=job, result=payload.result)
            if job.job_type == "prepare_new_card":
                return _persist_new_card_prepare(db, job=job, result=payload.result)
            if job.job_type == "map_new_card_category":
                return _persist_new_card_mapping(db, job=job, result=payload.result)
            if job.job_type == "create_new_card":
                return _persist_new_card_import(db, job=job, result=payload.result)
            if job.job_type == "confirm_new_card":
                return _persist_new_card_confirmation(db, job=job, result=payload.result)
            return _persist_product_inspection(db, job=job, result=payload.result)
        except ValueError as exc:
            job.status = "failed"
            job.error_code = "invalid_agent_result"
            job.error_message = str(exc)[:4000]
            job.completed_at = _now()
            if job.item_id is not None:
                item = db.scalar(select(ProductTestItem).where(ProductTestItem.id == job.item_id).with_for_update())
                if item is not None:
                    if _is_new_card_item(item) and job.job_type == "confirm_new_card":
                        followup = _reschedule_new_card_confirmation(
                            db,
                            job=job,
                            item=item,
                            last_error=job.error_message,
                        )
                        if followup is not None:
                            db.commit()
                            return {
                                **_job_payload(job),
                                "retry_job": _job_payload(followup),
                            }
                    item.status = "new_card_error" if _is_new_card_item(item) else "error"
                    item.last_error = job.error_message
                    if job.job_type == "create_offer":
                        hide_after = _category_rejection_hide_after(
                            error_code=job.error_code,
                            error_message=job.error_message,
                        )
                        _set_kaspi_submission(
                            item,
                            status="failed",
                            completed_at=_now().isoformat(),
                            hide_after=hide_after,
                            terminal_rejection=bool(hide_after),
                            error=item.last_error,
                            error_code=job.error_code,
                        )
                    elif _is_new_card_item(item) and job.job_type in {"create_new_card", "confirm_new_card"}:
                        _set_kaspi_submission(
                            item,
                            route="new_card",
                            stage="product_import" if job.job_type == "create_new_card" else "moderation",
                            status="failed",
                            completed_at=_now().isoformat(),
                            hide_after=None,
                            terminal_rejection=False,
                            error=item.last_error,
                            error_code=job.error_code,
                        )
            db.commit()
            return _job_payload(job)
