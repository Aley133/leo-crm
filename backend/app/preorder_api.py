"""Explicit Kaspi preorder connection through existing Product Test agent jobs."""

import re
from hashlib import sha256
from decimal import Decimal
from urllib.parse import urlsplit
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, update
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import Product, ProductStatus
from .product_test_models import ProductTestItem, ProductTestJob
from .fast_dumping_models import FastDumpingPolicy, FastDumpingJob
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .workspace_context import current_workspace_id
from .workspace_models import Workspace

router = APIRouter(
    prefix="/api/preorder",
    tags=["preorder"],
    dependencies=[Depends(require_service_token)],
)
PREFIX = "manual-preorder:"
ARCHIVE_AGENT_VERSION = "1.1.25"


def supports_archive_preorder(version: str | None) -> bool:
    return bool(version and re.fullmatch(r"\d+\.\d+\.\d+", version)
                and tuple(map(int, version.split("."))) >= (1, 1, 25))


class PreorderRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=2000)
    price_kzt: int = Field(gt=0, le=100000000)
    preorder_days: int = Field(ge=1, le=365)
    stock_count: int = Field(default=5, ge=1, le=1000000)
    city_id: str = Field(default="196220100", min_length=1, max_length=32)
    zone_id: str = Field(default="Magnum_ZONE1", min_length=1, max_length=64)


class PreorderEdit(BaseModel):
    price_kzt: int = Field(gt=0, le=100000000)
    preorder_days: int = Field(ge=1, le=365)
    stock_count: int | None = Field(default=None, ge=1, le=1000000)


def resolve_reference(db: Session, *, workspace: int, reference: str) -> tuple[str, str]:
    reference = reference.strip()
    if "://" in reference or "/" in reference:
        return card_id(reference), reference
    if not reference or len(reference) > 128:
        raise HTTPException(422, "Укажите SKU или ссылку на карточку Kaspi")
    # Merchant SKU is not necessarily a master ID. Exact local mappings take
    # precedence over numeric prefixes, and never cross workspace boundaries.
    ids = set(db.scalars(select(Product.kaspi_product_id).where(
        Product.workspace_id == workspace, Product.merchant_sku == reference
    )).all())
    ids.update(db.scalars(select(ProductTestItem.kaspi_product_id).where(
        ProductTestItem.workspace_id == workspace,
        ProductTestItem.merchant_sku == reference,
    )).all())
    ids = {re.sub(r"^(\d{5,18})_[^\s/]+$", r"\1", str(value)) for value in ids if value}
    if len(ids) > 1:
        raise HTTPException(409, "SKU связан с несколькими карточками; устраните дубликаты")
    master = next(iter(ids), None)
    # Older XML imports stored the entire merchant SKU in kaspi_product_id.
    # Keep its product/history, but resolve the actual master in the cabinet.
    match = re.fullmatch(r"(\d{5,18})(?:_[^\s/]{1,109})?", str(master or reference))
    master = match.group(1) if match else None
    if not master:
        return "sku:" + sha256(reference.encode()).hexdigest()[:32], reference
    # Prefer a previously confirmed URL to avoid rediscovering an old card.
    from .fast_dumping_models import FastDumpingState

    urls = db.scalars(select(ProductTestItem.kaspi_url).where(
        ProductTestItem.workspace_id == workspace,
        ProductTestItem.kaspi_product_id == master,
    )).all()
    urls.extend(db.scalars(select(FastDumpingState.product_url).join(
        Product, Product.id == FastDumpingState.product_id
    ).where(Product.workspace_id == workspace, Product.kaspi_product_id == master)).all())
    for url in urls:
        if not url:
            continue
        try:
            if card_id(url) == master:
                return master, url
        except HTTPException:
            continue
    # The existing agent resolves a master ID to the real public card URL.
    return master, master


def card_id(reference: str) -> str:
    try:
        parts = urlsplit(reference.strip())
        port = parts.port
    except ValueError as exc:
        raise HTTPException(422, "Некорректная ссылка Kaspi") from exc
    match = re.fullmatch(r"/shop/p/[^/]+-(\d{5,18})/?", parts.path)
    if (
        parts.scheme != "https"
        or parts.hostname != "kaspi.kz"
        or port not in (None, 443)
        or parts.username
        or parts.password
        or not match
    ):
        raise HTTPException(
            422, "Вставьте полную ссылку https://kaspi.kz/shop/p/… на карточку товара"
        )
    return match.group(1)


def assert_no_preorder_write(db: Session, *, product: Product) -> None:
    pending = db.scalar(
        select(ProductTestJob.id)
        .join(ProductTestItem, ProductTestItem.id == ProductTestJob.item_id)
        .where(
            ProductTestJob.workspace_id == product.workspace_id,
            ProductTestItem.workspace_id == product.workspace_id,
            ProductTestItem.input_reference.startswith(PREFIX),
            or_(ProductTestItem.product_id == product.id,
                ProductTestItem.kaspi_product_id == product.kaspi_product_id),
            ProductTestJob.status.in_(("queued", "leased")),
        )
        .limit(1)
    )
    if pending:
        raise HTTPException(
            409, "Дождитесь подключения предзаказа к Kaspi перед изменением демпинга"
        )


def _check_existing(db: Session, *, workspace: int, kaspi_id: str, merchant_sku: str | None = None) -> Product | None:
    from .dumping_service import physical_stock_counts
    from .product_inventory_group import inventory_owner_ids_for_products

    products = db.scalars(
        select(Product)
        .where(Product.workspace_id == workspace, or_(
            Product.kaspi_product_id == kaspi_id,
            Product.kaspi_product_id.startswith(kaspi_id + "_", autoescape=True),
            Product.merchant_sku == merchant_sku if merchant_sku else False,
        ))
        .with_for_update()
    ).all()
    if len(products) > 1:
        raise HTTPException(
            409, "В CRM несколько товаров с этим Kaspi ID; сначала устраните дубликаты"
        )
    if not products:
        return None
    product = products[0]
    owners = inventory_owner_ids_for_products(db, {product.id})
    if (
        physical_stock_counts(
            db, product_ids={product.id}, owner_by_product=owners
        ).get(product.id, 0)
        > 0
    ):
        raise HTTPException(
            409, "У товара есть физический остаток. Его нельзя заменять предзаказом"
        )
    fast = db.scalar(
        select(FastDumpingPolicy)
        .where(
            FastDumpingPolicy.workspace_id == workspace,
            FastDumpingPolicy.product_id == product.id,
        )
        .with_for_update()
    )
    classic = db.scalar(
        select(DumpingPolicy)
        .where(DumpingPolicy.product_id == product.id)
        .with_for_update()
    )
    pending = db.scalar(
        select(FastDumpingJob.id)
        .where(
            FastDumpingJob.workspace_id == workspace,
            FastDumpingJob.product_id == product.id,
            FastDumpingJob.status.in_(
                ("queued_apply", "leased_apply", "queued_verify", "leased_verify")
            ),
        )
        .limit(1)
    )
    legacy_pending = db.scalar(
        select(DumpingRun.id)
        .where(
            DumpingRun.workspace_id == workspace,
            DumpingRun.product_id == product.id,
            DumpingRun.status.in_(("queued_local", "leased_local")),
        )
        .limit(1)
    )
    if (
        (fast and fast.enabled)
        or (classic and classic.enabled)
        or pending
        or legacy_pending
    ):
        active_modes = []
        if fast and fast.enabled:
            active_modes.append("полная автоматизация" if fast.pricing_mode == "automation" else "быстрый демпинг")
        if classic and classic.enabled:
            active_modes.append("классический демпинг")
        detail = (
            "Изменения не отправлены агенту: включён " + ", ".join(active_modes)
            + ". Сначала выключите его в настройках этого товара и повторите подключение."
            if active_modes else
            "Изменения не отправлены агенту: Kaspi ещё подтверждает предыдущую операцию демпинга. Дождитесь её завершения и повторите попытку."
        )
        raise HTTPException(
            409,
            detail,
            headers={"X-Preorder-Product-Id": str(product.id),
                     "X-Preorder-Fast-Enabled": "1" if fast and fast.enabled else "0",
                     "X-Preorder-Classic-Enabled": "1" if classic and classic.enabled else "0"},
        )
    return product


@router.get("")
def read_preorders(db: Session = Depends(get_db)):
    from .product_test_api import _item_payload, _product_test_agent_status

    workspace = current_workspace_id()
    items = db.scalars(
        select(ProductTestItem)
        .where(
            ProductTestItem.workspace_id == workspace,
            ProductTestItem.input_reference.startswith(PREFIX),
        )
        .order_by(ProductTestItem.updated_at.desc(), ProductTestItem.id.desc())
        .limit(100)
    ).all()
    return {
        "items": [_item_payload(i) for i in items],
        "agent": _product_test_agent_status(workspace),
        "required_agent_version": ARCHIVE_AGENT_VERSION,
    }


@router.post("/products/{product_id}/stop-classic")
def stop_classic_for_preorder(product_id: int, db: Session = Depends(get_db)):
    """Explicit owner action for legacy settings no longer exposed by the UI."""
    from .dumping_api import DumpingPolicyUpsert, upsert_dumping_policy

    workspace = current_workspace_id()
    db.scalar(select(Workspace).where(Workspace.id == workspace).with_for_update())
    product = db.scalar(select(Product).where(
        Product.id == product_id, Product.workspace_id == workspace
    ).with_for_update())
    if product is None:
        raise HTTPException(404, "Товар не найден в текущем магазине")
    assert_no_preorder_write(db, product=product)
    policy = db.scalar(select(DumpingPolicy).where(
        DumpingPolicy.product_id == product_id
    ).with_for_update())
    if policy is None:
        return {"product_id": product_id, "enabled": False}
    values = {field: getattr(policy, field) for field in DumpingPolicyUpsert.model_fields}
    values.update(enabled=False, auto_publish_xml=False)
    # Preserve thresholds and use the established manual-disable history so
    # legacy automatic recovery cannot re-enable the policy behind the owner.
    return upsert_dumping_policy(product_id, DumpingPolicyUpsert(**values), db)


@router.post("")
def connect_preorder(payload: PreorderRequest, db: Session = Depends(get_db)):
    from .product_test_api import _queue_job, _item_payload, _job_payload

    workspace = current_workspace_id()
    db.scalar(select(Workspace).where(Workspace.id == workspace).with_for_update())
    kaspi_id, inspection_reference = resolve_reference(
        db, workspace=workspace, reference=payload.reference
    )
    source_sku = payload.reference.strip() if "://" not in payload.reference else None
    product = _check_existing(db, workspace=workspace, kaspi_id=kaspi_id, merchant_sku=source_sku)
    if product is not None:
        classic = db.scalar(
            select(DumpingPolicy)
            .where(DumpingPolicy.product_id == product.id)
            .with_for_update()
        )
        if classic is not None:
            classic.auto_publish_xml = False
    items = db.scalars(
        select(ProductTestItem)
        .where(
            ProductTestItem.workspace_id == workspace,
            or_(ProductTestItem.kaspi_product_id == kaspi_id,
                ProductTestItem.merchant_sku == source_sku if source_sku else False),
        )
        .with_for_update()
    ).all()
    if len(items) > 1:
        raise HTTPException(409, "В Тесте товара несколько записей этой карточки; устраните дубликаты")
    item = items[0] if items else None
    if item is not None:
        pending = db.scalar(
            select(ProductTestJob.id)
            .where(
                ProductTestJob.workspace_id == workspace,
                ProductTestJob.item_id == item.id,
                ProductTestJob.status.in_(("queued", "leased")),
            )
            .limit(1)
        )
        if pending:
            raise HTTPException(
                409, "Эта карточка уже подключается. Дождитесь результата"
            )
    else:
        item = ProductTestItem(
            workspace_id=workspace,
            input_reference=PREFIX + kaspi_id,
            kaspi_product_id=kaspi_id,
            merchant_sku=PREFIX + kaspi_id,
            name="Карточка Kaspi " + kaspi_id,
            kaspi_url=payload.reference.strip(),
        )
        db.add(item)
        db.flush()
    item.kaspi_product_id = kaspi_id
    item.input_reference = PREFIX + kaspi_id
    item.kaspi_url = inspection_reference
    item.city_id = payload.city_id.strip()
    item.zone_id = payload.zone_id.strip()
    item.test_price_kzt = Decimal(payload.price_kzt)
    item.preorder_days = payload.preorder_days
    item.stock_count = payload.stock_count
    item.product_id = product.id if product else None
    item.status = "preorder_inspecting"
    item.active = False
    item.last_error = None
    item.offers_json = {**(item.offers_json or {}), "mode": "manual_preorder", "manual_preorder": True,
                        "source_sku": source_sku}
    job = _queue_job(
        db,
        workspace_id=workspace,
        job_type="inspect",
        reference=item.kaspi_url,
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options={"manual_preorder": True, "preorder_archive": True,
                 "master_sku": None if kaspi_id.startswith("sku:") else kaspi_id,
                 "source_sku": source_sku, "product_name": product.name if product else None},
    )
    db.commit()
    return {"item": _item_payload(item), "job": _job_payload(job)}


@router.patch("/{item_id}")
def edit_preorder(
    item_id: int, payload: PreorderEdit, db: Session = Depends(get_db)
):
    item = db.scalar(
        select(ProductTestItem).where(
            ProductTestItem.id == item_id,
            ProductTestItem.workspace_id == current_workspace_id(),
            ProductTestItem.input_reference.startswith(PREFIX),
        )
    )
    if item is None:
        raise HTTPException(404, "Предзаказ не найден в текущем магазине")
    # Reuse the same workspace/product/item lock order and the exact existing
    # offer confirmation pipeline. Never change XML before Kaspi confirms it.
    return connect_preorder(
        PreorderRequest(
            reference=(item.offers_json or {}).get("source_sku") or item.kaspi_url,
            price_kzt=payload.price_kzt,
            preorder_days=payload.preorder_days,
            stock_count=payload.stock_count if payload.stock_count is not None else item.stock_count,
            city_id=item.city_id,
            zone_id=item.zone_id,
        ),
        db,
    )


def persist_preorder_inspection(db: Session, *, job: ProductTestJob, result: dict):
    from .product_test_api import _queue_job, _finish_job, _item_payload, _job_payload
    from .product_images import normalize_product_image_url

    db.scalar(
        select(Workspace).where(Workspace.id == job.workspace_id).with_for_update()
    )
    item = db.scalar(
        select(ProductTestItem)
        .where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        )
        .with_for_update()
    )
    master = str(result.get("kaspi_product_id") or "")
    unresolved = item is not None and item.kaspi_product_id != master
    catalog = result.get("catalog_state") or {}
    source_sku = (job.options_json or {}).get("source_sku")
    cabinet_proof = (catalog.get("found") and str(catalog.get("master_sku")) == master
                     and source_sku and source_sku in (catalog.get("sku"), catalog.get("master_sku")))
    if (item is None or not re.fullmatch(r"\d{5,18}", master)
        or not result.get("product_name") or (unresolved and not cabinet_proof)):
        raise ValueError("Agent не подтвердил выбранную карточку Kaspi")
    try:
        product = _check_existing(db, workspace=job.workspace_id, kaspi_id=master,
                                  merchant_sku=catalog.get("sku"))
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc
    if unresolved:
        existing = db.scalars(select(ProductTestItem).where(
            ProductTestItem.workspace_id == job.workspace_id,
            ProductTestItem.id != item.id,
            or_(ProductTestItem.kaspi_product_id == master,
                ProductTestItem.merchant_sku == catalog["sku"]),
        ).with_for_update()).all()
        if len(existing) > 1:
            raise ValueError("В CRM несколько записей выбранной карточки")
        if existing:
            target = existing[0]
            if db.scalar(select(ProductTestJob.id).where(
                ProductTestJob.workspace_id == job.workspace_id,
                ProductTestJob.item_id == target.id,
                ProductTestJob.status.in_(("queued", "leased")),
            ).limit(1)):
                raise ValueError("Эта карточка уже подключается. Дождитесь результата")
            for field in ("test_price_kzt", "preorder_days", "stock_count", "city_id", "zone_id"):
                setattr(target, field, getattr(item, field))
            target.offers_json = {**(target.offers_json or {}), **item.offers_json}
            db.execute(update(ProductTestJob).where(
                ProductTestJob.workspace_id == job.workspace_id, ProductTestJob.item_id == item.id
            ).values(item_id=target.id))
            db.delete(item)
            db.flush()
            item = target
    item.kaspi_product_id = master
    item.input_reference = PREFIX + master
    item.active = False
    item.product_id = product.id if product else None
    if catalog.get("found"):
        item.offers_json = {**item.offers_json, "existing_merchant_sku": catalog["sku"]}
    item.name = str(result["product_name"])[:500]
    product_url = str(result.get("product_url") or "")
    if product_url:
        try:
            if card_id(product_url) == item.kaspi_product_id:
                item.kaspi_url = product_url
        except HTTPException:
            pass
    item.brand = str(result.get("brand") or item.brand or "")[:255] or None
    item.image_url = normalize_product_image_url(result.get("image_url")) or item.image_url
    item.status = "adding_to_kaspi"
    followup = _queue_job(
        db,
        workspace_id=job.workspace_id,
        job_type="create_offer",
        reference=f"item:{item.id}",
        item_id=item.id,
        city_id=item.city_id,
        zone_id=item.zone_id,
        options={
            "manual_preorder": True,
            "preorder_archive": bool((job.options_json or {}).get("preorder_archive")),
            "master_sku": item.kaspi_product_id,
            "merchant_sku": (item.offers_json or {}).get("existing_merchant_sku"),
            "model": item.name,
            "initial_price_kzt": int(item.test_price_kzt),
            "stock_count": item.stock_count,
            "preorder_days": item.preorder_days,
        },
    )
    _finish_job(job, {"item_id": item.id, "create_job_id": followup.id})
    db.commit()
    return {"job": _job_payload(job), "item": _item_payload(item)}


def enroll_preorder(db: Session, *, job: ProductTestJob, result: dict):
    from .product_test_api import (
        _finish_job,
        _item_payload,
        _job_payload,
        _settings,
        _now,
        build_product_test_xml,
    )
    from .fast_dumping_service import ensure_state, cancel_active_job

    db.scalar(
        select(Workspace).where(Workspace.id == job.workspace_id).with_for_update()
    )
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(
            KaspiXmlFeed.workspace_id == job.workspace_id, KaspiXmlFeed.active.is_(True)
        )
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
        .with_for_update()
    )
    item = db.scalar(
        select(ProductTestItem)
        .where(
            ProductTestItem.id == job.item_id,
            ProductTestItem.workspace_id == job.workspace_id,
        )
        .with_for_update()
    )
    state = result.get("after") or result.get("before") or {}
    sku = str(result.get("merchant_sku") or state.get("sku") or "").strip()
    if (
        item is None
        or not sku
        or len(sku) > 128
        or ((job.options_json or {}).get("merchant_sku")
            and sku != job.options_json["merchant_sku"])
        or not state.get("found")
        or str(result.get("master_sku") or state.get("master_sku"))
        != item.kaspi_product_id
        or Decimal(str(state.get("price_kzt") or 0)) != item.test_price_kzt
        or int(state.get("preorder_days") or 0) != item.preorder_days
        or int(state.get("stock_count") or 0) != item.stock_count
        or ((job.options_json or {}).get("preorder_archive") and (
            state.get("row_available") is False
            or state.get("nested_available") in ("no", "false", "0")
            or state.get("query_mode") == "inactive"))
    ):
        raise ValueError(
            "Kaspi не подтвердил карточку, цену, остаток и срок предзаказа"
        )
    try:
        product = _check_existing(
            db, workspace=job.workspace_id, kaspi_id=item.kaspi_product_id, merchant_sku=sku
        )
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc
    collision = db.scalar(
        select(Product.id)
        .where(
            Product.workspace_id == job.workspace_id,
            Product.merchant_sku == sku,
            Product.kaspi_product_id != item.kaspi_product_id,
            Product.id != product.id if product else True,
        )
        .limit(1)
    )
    item_collision = db.scalar(
        select(ProductTestItem.id)
        .where(
            ProductTestItem.workspace_id == job.workspace_id,
            ProductTestItem.merchant_sku == sku,
            ProductTestItem.id != item.id,
        )
        .limit(1)
    )
    if collision or item_collision:
        raise ValueError("Этот Merchant SKU уже привязан к другой записи CRM")
    # Validate the mirror before touching the CRM product, so a malformed XML
    # cannot leave a half-enrolled product behind in the agent error handler.
    from types import SimpleNamespace

    xml_item = SimpleNamespace(
        active=True,
        test_price_kzt=item.test_price_kzt,
        merchant_sku=sku,
        name=item.name,
        brand=item.brand,
        preorder_days=item.preorder_days,
        stock_count=item.stock_count,
        city_id=item.city_id,
    )
    try:
        mirror = (
            build_product_test_xml(
                feed.generated_xml or feed.source_xml, [xml_item]
            ).decode()
            if feed
            else None
        )
        source_mirror = (
            build_product_test_xml(feed.source_xml, [xml_item]).decode()
            if feed
            else None
        )
    except (ValueError, ElementTree.ParseError) as exc:
        raise ValueError(
            "Не удалось сохранить XML-зеркало предзаказа: " + str(exc)
        ) from exc
    item.merchant_sku = sku
    if product is None:
        product = Product(
            workspace_id=job.workspace_id,
            kaspi_product_id=item.kaspi_product_id,
            merchant_sku=sku,
            name=item.name,
            brand=item.brand,
            image_url=item.image_url,
            status=ProductStatus.ACTIVE.value,
            sale_enabled=True,
        )
        db.add(product)
        db.flush()
    else:
        product.kaspi_product_id = item.kaspi_product_id
        product.merchant_sku = sku
        product.name = item.name
        product.image_url = item.image_url or product.image_url
        product.status = ProductStatus.ACTIVE.value
        product.sale_enabled = True
    product.sale_state_overridden = False
    policy = db.scalar(
        select(FastDumpingPolicy)
        .where(
            FastDumpingPolicy.workspace_id == job.workspace_id,
            FastDumpingPolicy.product_id == product.id,
        )
        .with_for_update()
    )
    if policy is None:
        settings = _settings(db, job.workspace_id)
        policy = FastDumpingPolicy(
            workspace_id=job.workspace_id,
            product_id=product.id,
            enabled=False,
            minimum_profit_kzt=settings.minimum_profit_kzt,
            city_id=item.city_id,
            zone_id=item.zone_id,
        )
        db.add(policy)
        db.flush()
    state_row = ensure_state(db, policy=policy, workspace_id=job.workspace_id)
    cancel_active_job(db, state=state_row, reason="Подключён ручной предзаказ")
    policy.enabled = False
    policy.city_id = item.city_id
    policy.zone_id = item.zone_id
    state_row.status = "paused"
    state_row.status_reason = "Предзаказ подключён по указанной цене. Настройте себестоимость и включите демпинг при необходимости."
    state_row.own_price_kzt = item.test_price_kzt
    state_row.next_scan_at = None
    if mirror:
        feed.generated_xml = mirror
        feed.source_xml = source_mirror
        feed.generated_at = _now()
    item.product_id = product.id
    item.fast_dumping_policy_id = policy.id
    item.status = "enrolled_fast_dumping"
    item.added_at = _now()
    item.last_error = None
    _finish_job(
        job, {"product_id": product.id, "policy_id": policy.id, "merchant_sku": sku}
    )
    db.commit()
    return {
        "job": _job_payload(job),
        "item": _item_payload(item),
        "product_id": product.id,
        "fast_dumping_policy_id": policy.id,
    }
