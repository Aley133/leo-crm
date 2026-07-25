from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .dumping_competitor_worker import enqueue_competitor_scan
from .dumping_models import DumpingPolicy, KaspiXmlFeed
from .dumping_service import decide_dumping_price, publish_decision
from .kaspi_offer_competitor import scan_kaspi_competitors
from .models import Product
from .suppliers import ProductBinding


async def execute_dumping_for_product(db: Session, product_id: int) -> dict:
    """Execute one competitor scan and publish one pricing decision.

    This function is called by the dedicated dumping competitor worker only.
    It does not create Browser Agent jobs and does not depend on the supplier
    browser process.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError("Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise ValueError("Демпинг для товара не подключён")
    feed = db.scalar(select(KaspiXmlFeed).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None or not feed.merchant_id:
        raise ValueError("Импортируйте полный XML с merchantid")

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
    return {
        "run_id": run.id,
        "feed_url": "/feeds/kaspi/catalog.xml",
        "market": {
            "own_price_kzt": market.own_price_kzt,
            "competitor_price_kzt": market.competitor_price_kzt,
            "competitor_name": market.competitor_name,
            "own_position": market.own_position,
            "seller_count": market.seller_count,
            "product_url": market.product_url,
        },
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


def refresh_dumping_for_supplier_product(supplier_product_id: int) -> None:
    """Queue enabled products after a committed supplier observation.

    Supplier ingestion remains authoritative and finishes first. This function
    only resolves product IDs and places them into the independent competitor
    queue. It never calls Kaspi and never interacts with Browser Agent jobs.
    """
    with SessionLocal() as db:
        product_ids = list(
            db.scalars(
                select(ProductBinding.product_id)
                .join(DumpingPolicy, DumpingPolicy.product_id == ProductBinding.product_id)
                .where(
                    ProductBinding.supplier_product_id == supplier_product_id,
                    ProductBinding.status.in_(("active", "confirmed", "degraded")),
                    DumpingPolicy.enabled.is_(True),
                    DumpingPolicy.auto_publish_xml.is_(True),
                )
                .distinct()
            )
        )
    for product_id in product_ids:
        enqueue_competitor_scan(product_id, reason="supplier_snapshot_changed")
