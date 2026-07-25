from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .dumping_service import decide_dumping_price, publish_decision, resolve_cost_source
from .kaspi_offer_competitor import scan_kaspi_competitors
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
        source = resolve_cost_source(db, product_id=product.id, inventory_first=policy.inventory_first)
        result.append({
            "product_id": product.id,
            "name": product.name,
            "kaspi_product_id": product.kaspi_product_id,
            "merchant_sku": product.merchant_sku,
            "policy": policy,
            "source": None if source is None else {
                "kind": source.kind,
                "name": source.name,
                "unit_cost_kzt": source.unit_cost_kzt,
                "delivery_days": source.delivery_days,
            },
            "latest_run": latest,
        })
    return result


@router.get("/products/{product_id}")
def read_dumping_policy(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    latest = db.scalar(
        select(DumpingRun).where(DumpingRun.product_id == product_id).order_by(DumpingRun.id.desc()).limit(1)
    )
    source = None if policy is None else resolve_cost_source(
        db, product_id=product_id, inventory_first=policy.inventory_first
    )
    return {
        "product": product,
        "policy": policy,
        "source": None if source is None else {
            "kind": source.kind,
            "name": source.name,
            "unit_cost_kzt": source.unit_cost_kzt,
            "delivery_days": source.delivery_days,
        },
        "latest_run": latest,
    }


@router.put("/products/{product_id}")
def upsert_dumping_policy(
    product_id: int,
    payload: DumpingPolicyUpsert,
    db: Session = Depends(get_db),
):
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
    return policy


@router.post("/products/{product_id}/run-now")
async def run_dumping_now(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=409, detail="Демпинг для товара не подключён")
    feed = db.scalar(select(KaspiXmlFeed).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None or not feed.merchant_id:
        raise HTTPException(status_code=409, detail="Импортируйте полный XML с merchantid")

    try:
        market = await scan_kaspi_competitors(
            product,
            own_merchant_id=feed.merchant_id,
            city_id=policy.city_id,
            zone_id=policy.zone_id,
        )
        decision = decide_dumping_price(
            db,
            product=product,
            policy=policy,
            competitor_price_kzt=market.competitor_price_kzt,
            own_price_kzt=market.own_price_kzt,
        )
        run = publish_decision(db, product=product, policy=policy, decision=decision)
        run.explanation_json = {
            **run.explanation_json,
            "competitor_name": market.competitor_name,
            "own_position": market.own_position,
            "seller_count": market.seller_count,
            "product_url": market.product_url,
        }
        db.commit()
        db.refresh(run)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=f"Kaspi competitor scan failed: {exc}") from exc
    return {
        "run": run,
        "feed_url": "/feeds/kaspi/catalog.xml",
        "decision": {
            "source_kind": decision.source.kind,
            "source_name": decision.source.name,
            "source_cost_kzt": decision.source.unit_cost_kzt,
            "safe_floor_kzt": decision.safe_floor_kzt,
            "competitor_price_kzt": decision.competitor_price_kzt,
            "target_price_kzt": decision.target_price_kzt,
            "preorder_days": decision.preorder_days,
            "status": decision.status,
        },
    }


@public_router.get("/feeds/kaspi/catalog.xml", response_class=Response)
def kaspi_catalog_feed(db: Session = Depends(get_db)) -> Response:
    feed = db.scalar(select(KaspiXmlFeed).where(KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None:
        raise HTTPException(status_code=404, detail="Kaspi XML feed is not configured")
    return Response(
        content=feed.generated_xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Cache-Control": "no-store, max-age=0"},
    )
