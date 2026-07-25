from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .dumping_runner import execute_dumping_for_product
from .dumping_service import resolve_cost_source
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
        # A broken or partially migrated supplier binding must not make the
        # whole dumping workspace unavailable. The card remains visible and
        # the operator can fix its source separately.
        return None, str(exc)
    if source is None:
        return None, None
    return {
        "kind": source.kind,
        "name": source.name,
        "unit_cost_kzt": source.unit_cost_kzt,
        "delivery_days": source.delivery_days,
    }, None


@router.get("")
def list_dumping_products(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(DumpingPolicy, Product)
        .join(Product, Product.id == DumpingPolicy.product_id)
        .order_by(DumpingPolicy.updated_at.desc(), DumpingPolicy.id.desc())
    ).all()
    result: list[dict] = []
    for policy, product in rows:
        latest = db.scalar(
            select(DumpingRun)
            .where(DumpingRun.product_id == product.id)
            .order_by(DumpingRun.id.desc())
            .limit(1)
        )
        source, source_error = _source_payload(db, policy)
        result.append({
            "product_id": product.id,
            "name": product.name,
            "kaspi_product_id": product.kaspi_product_id,
            "merchant_sku": product.merchant_sku,
            "policy": _policy_payload(policy),
            "source": source,
            "source_error": source_error,
            "latest_run": _run_payload(latest),
        })
    return result


@router.get("/products/{product_id}")
def read_dumping_policy(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    latest = db.scalar(
        select(DumpingRun)
        .where(DumpingRun.product_id == product_id)
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    source = None
    source_error = None
    if policy is not None:
        source, source_error = _source_payload(db, policy)
    return {
        "product": _product_payload(product),
        "policy": _policy_payload(policy),
        "source": source,
        "source_error": source_error,
        "latest_run": _run_payload(latest),
    }


@router.put("/products/{product_id}")
def upsert_dumping_policy(
    product_id: int,
    payload: DumpingPolicyUpsert,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    values = payload.model_dump()
    if policy is None:
        policy = DumpingPolicy(product_id=product_id, **values)
        db.add(policy)
    else:
        for key, value in values.items():
            setattr(policy, key, value)
    db.commit()
    db.refresh(policy)
    return _policy_payload(policy) or {}


@router.post("/products/{product_id}/run-now")
async def run_dumping_now(product_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return await execute_dumping_for_product(db, product_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Kaspi competitor scan failed: {exc}") from exc


@public_router.get("/feeds/kaspi/catalog.xml", response_class=Response)
def kaspi_catalog_feed(db: Session = Depends(get_db)) -> Response:
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(KaspiXmlFeed.active.is_(True))
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    if feed is None:
        raise HTTPException(status_code=404, detail="Kaspi XML feed is not configured")
    return Response(
        content=feed.generated_xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Cache-Control": "no-store, max-age=0"},
    )
