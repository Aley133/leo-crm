from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_competitor_worker import enqueue_competitor_scan, state_for_product
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .dumping_service import calculate_safe_floor, resolve_cost_source
from .models import Product


class DumpingPolicyUpsert(BaseModel):
    enabled: bool = True
    minimum_profit_kzt: Decimal = Field(default=1000, ge=0)
    undercut_step_kzt: int = Field(default=1, ge=1, le=10000)
    supplier_delivery_buffer_days: int = Field(default=1, ge=0, le=30)
    inventory_first: bool = True
    auto_publish_xml: bool = True
    city_id: str = Field(default="750000000", min_length=1, max_length=32)
    zone_id: str = Field(default="Magnum_ZONE1", min_length=1, max_length=64)


class DumpingRuntimeRunRow(BaseModel):
    job_id: int
    product_id: int
    kaspi_product_id: str
    merchant_sku: str | None
    product_name: str
    status: str
    stage: str
    agent_id: str | None
    lease_until: datetime | None
    started_at: datetime
    updated_at: datetime
    detail: str


class DumpingRuntimeSnapshot(BaseModel):
    active_runs: list[DumpingRuntimeRunRow]
    queued_count: int
    latest_run: DumpingRuntimeRunRow | None
    checked_at: datetime


router = APIRouter(
    prefix="/api/dumping",
    tags=["dumping"],
    dependencies=[Depends(require_service_token)],
)
public_router = APIRouter(tags=["dumping-feed"], include_in_schema=True)


def _policy_payload(policy: DumpingPolicy | None) -> dict | None:
    if policy is None:
        return None
    return {
        "id": policy.id,
        "product_id": policy.product_id,
        "enabled": policy.enabled,
        "minimum_profit_kzt": policy.minimum_profit_kzt,
        "undercut_step_kzt": policy.undercut_step_kzt,
        "supplier_delivery_buffer_days": policy.supplier_delivery_buffer_days,
        "inventory_first": policy.inventory_first,
        "auto_publish_xml": policy.auto_publish_xml,
        "city_id": policy.city_id,
        "zone_id": policy.zone_id,
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
    }


def _run_payload(run: DumpingRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "product_id": run.product_id,
        "dumping_policy_id": run.dumping_policy_id,
        "status": run.status,
        "source_kind": run.source_kind,
        "source_name": run.source_name,
        "source_cost_kzt": run.source_cost_kzt,
        "source_delivery_days": run.source_delivery_days,
        "safe_floor_kzt": run.safe_floor_kzt,
        "own_price_kzt": run.own_price_kzt,
        "competitor_price_kzt": run.competitor_price_kzt,
        "target_price_kzt": run.target_price_kzt,
        "preorder_days": run.preorder_days,
        "published": run.published,
        "explanation_json": run.explanation_json or {},
        "created_at": run.created_at,
    }


def _runtime_datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            result = fallback
    else:
        result = fallback
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


def _runtime_status(run: DumpingRun, *, lease_until: datetime | None) -> tuple[str, str]:
    metadata = run.explanation_json or {}
    if run.status == "queued_local":
        return "queued", "Ожидает свободный Kaspi Competitor Agent"
    if run.status == "leased_local":
        now = datetime.now(UTC)
        if lease_until is not None and lease_until < now:
            return "lease_expired", "Lease истёк — Kaspi Agent не подтвердил завершение"
        return "processing", "Получает продавцов Kaspi и рассчитывает безопасную цену"
    if run.status == "succeeded_local":
        return "succeeded", "Проверка завершена, результат сохранён"
    error = metadata.get("error_message") or metadata.get("error_code")
    return "failed", str(error or "Проверка завершилась с ошибкой")


def _runtime_row(run: DumpingRun, product: Product) -> DumpingRuntimeRunRow:
    metadata = run.explanation_json or {}
    started_at = _runtime_datetime(
        metadata.get("leased_at"),
        fallback=_runtime_datetime(run.created_at, fallback=datetime.now(UTC)),
    )
    updated_at = _runtime_datetime(metadata.get("updated_at"), fallback=started_at)
    lease_until = (
        _runtime_datetime(metadata.get("lease_until"), fallback=started_at)
        if metadata.get("lease_until")
        else None
    )
    status_value, detail = _runtime_status(run, lease_until=lease_until)
    return DumpingRuntimeRunRow(
        job_id=run.id,
        product_id=product.id,
        kaspi_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        product_name=product.name,
        status=status_value,
        stage=str(metadata.get("stage") or status_value),
        agent_id=metadata.get("agent_id"),
        lease_until=lease_until,
        started_at=started_at,
        updated_at=updated_at,
        detail=detail,
    )


def _latest_run_or_none(db: Session, product_id: int) -> DumpingRun | None:
    """Read the latest run without making the whole workspace schema-fragile.

    Lightweight tests and partially migrated development databases may contain
    dumping policies before the dumping_runs table exists. In that state the
    policy workspace must remain readable and simply expose no run history.
    """
    try:
        return db.scalar(
            select(DumpingRun)
            .where(DumpingRun.product_id == product_id)
            .order_by(DumpingRun.id.desc())
            .limit(1)
        )
    except (OperationalError, ProgrammingError):
        db.rollback()
        return None


def _product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "kaspi_product_id": product.kaspi_product_id,
        "merchant_sku": product.merchant_sku,
        "brand": product.brand,
        "status": product.status,
    }


def _source_payload(db: Session, policy: DumpingPolicy) -> tuple[dict | None, str | None]:
    try:
        source = resolve_cost_source(
            db,
            product_id=policy.product_id,
            inventory_first=policy.inventory_first,
        )
    except Exception as exc:
        return None, str(exc)
    if source is None:
        return None, None
    return {
        "kind": source.kind,
        "name": source.name,
        "unit_cost_kzt": source.unit_cost_kzt,
        "delivery_days": source.delivery_days,
    }, None


def _pricing_preview(policy: DumpingPolicy, source: dict | None) -> dict | None:
    if source is None:
        return None
    floor = calculate_safe_floor(
        unit_cost_kzt=Decimal(source["unit_cost_kzt"]),
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    preorder_days = 0 if source["kind"] == "inventory" else int(source["delivery_days"] or 0) + int(policy.supplier_delivery_buffer_days)
    return {
        "safe_floor_kzt": floor,
        "preorder_days": preorder_days,
    }


@router.get("")
def list_dumping_products(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(DumpingPolicy, Product)
        .join(Product, Product.id == DumpingPolicy.product_id)
        .order_by(DumpingPolicy.updated_at.desc(), DumpingPolicy.id.desc())
    ).all()
    result: list[dict] = []
    for policy, product in rows:
        latest = _latest_run_or_none(db, product.id)
        source, source_error = _source_payload(db, policy)
        result.append({
            "product_id": product.id,
            "name": product.name,
            "kaspi_product_id": product.kaspi_product_id,
            "merchant_sku": product.merchant_sku,
            "policy": _policy_payload(policy),
            "source": source,
            "source_error": source_error,
            "pricing_preview": _pricing_preview(policy, source),
            "latest_run": _run_payload(latest),
            "scan_state": state_for_product(product.id, db=db),
        })
    return result


@router.get("/feed-status")
def read_dumping_feed_status(db: Session = Depends(get_db)) -> dict:
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(KaspiXmlFeed.active.is_(True))
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    if feed is None:
        product_count = int(db.scalar(select(func.count(Product.id))) or 0)
        return {
            "configured": False,
            "ready": False,
            "legacy_catalog_detected": product_count > 0,
            "product_count": product_count,
            "source_filename": None,
            "merchant_id": None,
            "imported_at": None,
            "generated_at": None,
            "feed_url": "/feeds/kaspi/catalog.xml",
        }
    return {
        "configured": True,
        "ready": bool(feed.merchant_id and feed.generated_xml),
        "legacy_catalog_detected": False,
        "product_count": int(db.scalar(select(func.count(Product.id))) or 0),
        "source_filename": feed.source_filename,
        "merchant_id": feed.merchant_id,
        "imported_at": feed.imported_at,
        "generated_at": feed.generated_at,
        "feed_url": "/feeds/kaspi/catalog.xml",
    }


@router.get("/runtime", response_model=DumpingRuntimeSnapshot)
def read_dumping_runtime(db: Session = Depends(get_db)) -> DumpingRuntimeSnapshot:
    active_rows = db.execute(
        select(DumpingRun, Product)
        .join(Product, Product.id == DumpingRun.product_id)
        .where(DumpingRun.status == "leased_local")
        .order_by(DumpingRun.id.desc())
    ).all()
    queued_count = int(
        db.scalar(
            select(func.count())
            .select_from(DumpingRun)
            .where(DumpingRun.status == "queued_local")
        )
        or 0
    )
    latest = db.execute(
        select(DumpingRun, Product)
        .join(Product, Product.id == DumpingRun.product_id)
        .where(
            DumpingRun.status.in_(
                ("queued_local", "leased_local", "succeeded_local", "failed_local")
            )
        )
        .order_by(DumpingRun.id.desc())
        .limit(1)
    ).first()
    return DumpingRuntimeSnapshot(
        active_runs=[_runtime_row(run, product) for run, product in active_rows],
        queued_count=queued_count,
        latest_run=None if latest is None else _runtime_row(latest[0], latest[1]),
        checked_at=datetime.now(UTC),
    )


@router.get("/products/{product_id}")
def read_dumping_policy(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    latest = _latest_run_or_none(db, product_id)
    source = None
    source_error = None
    if policy is not None:
        source, source_error = _source_payload(db, policy)
    return {
        "product": _product_payload(product),
        "policy": _policy_payload(policy),
        "source": source,
        "source_error": source_error,
        "pricing_preview": None if policy is None else _pricing_preview(policy, source),
        "latest_run": _run_payload(latest),
        "scan_state": state_for_product(product_id, db=db),
    }


@router.put("/products/{product_id}")
def upsert_dumping_policy(
    product_id: int,
    payload: DumpingPolicyUpsert,
    db: Session = Depends(get_db),
) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None:
        policy = DumpingPolicy(product_id=product_id)
        db.add(policy)
    for field, value in payload.model_dump().items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return {
        "product": _product_payload(product),
        "policy": _policy_payload(policy),
    }


@router.post("/products/{product_id}/run", status_code=status.HTTP_202_ACCEPTED)
def queue_dumping_run(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=409, detail="Демпинг для товара не подключён")
    try:
        queued = enqueue_competitor_scan(product_id, reason="manual")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "queued",
        "queued": queued,
        "product_id": product_id,
        "message": "Карточка поставлена в очередь локального Kaspi Competitor Agent",
    }


@public_router.get("/feeds/kaspi/catalog.xml", response_class=Response)
def read_public_kaspi_feed(db: Session = Depends(get_db)) -> Response:
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(KaspiXmlFeed.active.is_(True))
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    if feed is None or not feed.generated_xml:
        raise HTTPException(status_code=404, detail="Kaspi XML feed is not configured")
    return Response(content=feed.generated_xml, media_type="application/xml; charset=utf-8")
