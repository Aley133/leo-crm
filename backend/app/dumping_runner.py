from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .dumping_competitor_worker import enqueue_competitor_scan
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .dumping_service import (
    decide_dumping_price,
    feed_offer_is_expected_active,
    publish_decision,
    resolve_cost_source,
    suspend_product_removed_from_seller,
    suspend_product_without_cost_source,
    workspace_feed_url,
)
from .kaspi_offer_competitor import KaspiCompetitorSnapshot, scan_kaspi_competitors
from .models import Product
from .suppliers import ProductBinding
from .workspace_context import current_workspace_id, workspace_context


def apply_competitor_snapshot(
    db: Session,
    *,
    product_id: int,
    market: KaspiCompetitorSnapshot,
) -> dict:
    """Apply a market snapshot received from the local competitor agent.

    External Kaspi HTTP work is deliberately outside this function. The local
    competitor agent scans Kaspi from the seller network and returns only facts;
    CRM remains authoritative for pricing, audit and XML publication.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError("Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise ValueError("Демпинг для товара не подключён")

    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(
            KaspiXmlFeed.workspace_id == current_workspace_id(),
            KaspiXmlFeed.active.is_(True),
        )
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    expected_active = (
        None
        if feed is None
        else feed_offer_is_expected_active(
            feed.generated_xml or feed.source_xml,
            sku_candidates={product.merchant_sku or "", product.kaspi_product_id},
        )
    )
    previously_seen_own_offer = db.scalar(
        select(DumpingRun.id)
        .where(
            DumpingRun.product_id == product_id,
            DumpingRun.own_price_kzt.is_not(None),
        )
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    if (
        market.own_position is None
        and market.own_price_kzt is None
        and expected_active is True
        and previously_seen_own_offer is not None
    ):
        run = suspend_product_removed_from_seller(
            db,
            product=product,
            policy=policy,
        )
        return {
            "run_id": run.id,
            "feed_url": workspace_feed_url(db),
            "market": {
                "own_price_kzt": market.own_price_kzt,
                "competitor_price_kzt": market.competitor_price_kzt,
                "competitor_name": market.competitor_name,
                "own_position": market.own_position,
                "seller_count": market.seller_count,
                "product_url": market.product_url,
            },
            "decision": {
                "status": "suspended_seller_removed",
                "published": False,
                "automatic_recovery": False,
            },
        }

    decision = decide_dumping_price(
        db,
        product=product,
        policy=policy,
        competitor_price_kzt=market.competitor_price_kzt,
        own_price_kzt=market.own_price_kzt,
    )
    run = publish_decision(db, product=product, policy=policy, decision=decision)
    run.explanation_json = {
        **(run.explanation_json or {}),
        "competitor_name": market.competitor_name,
        "own_position": market.own_position,
        "seller_count": market.seller_count,
        "product_url": market.product_url,
        "scan_source": "local_kaspi_competitor_agent",
    }
    db.flush()
    return {
        "run_id": run.id,
        "feed_url": workspace_feed_url(db),
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
            "stock_count": decision.stock_count,
            "status": decision.status,
        },
    }


async def execute_dumping_for_product(db: Session, product_id: int) -> dict:
    """Legacy server-side scanner kept for diagnostics only.

    Production jobs are executed by the local Kaspi Competitor Agent and applied
    through :func:`apply_competitor_snapshot`.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError("Product not found")
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise ValueError("Демпинг для товара не подключён")
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(KaspiXmlFeed.workspace_id == current_workspace_id())
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    if feed is None or not feed.merchant_id:
        raise ValueError("Импортируйте полный XML с merchantid")

    market = await scan_kaspi_competitors(
        product,
        own_merchant_id=feed.merchant_id,
        city_id=policy.city_id,
        zone_id=policy.zone_id,
    )
    return apply_competitor_snapshot(db, product_id=product_id, market=market)


def refresh_dumping_for_supplier_product(
    supplier_product_id: int,
    *,
    workspace_id: int,
) -> None:
    """Apply supplier availability before queuing the next Kaspi price scan.

    A confirmed loss of the final procurement source is a safety event: close
    the XML offer immediately and keep the policy ready for automatic recovery.
    If inventory or another supplier is available, normal competitor scanning
    continues with that source.
    """
    with workspace_context(workspace_id):
        with SessionLocal() as db:
            product_ids = tuple(
                db.scalars(
                    select(ProductBinding.product_id)
                    .join(
                        DumpingPolicy,
                        DumpingPolicy.product_id == ProductBinding.product_id,
                    )
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
            with SessionLocal() as db:
                product = db.get(Product, product_id)
                policy = db.scalar(
                    select(DumpingPolicy)
                    .where(DumpingPolicy.product_id == product_id)
                    .with_for_update()
                )
                if product is None or policy is None or not policy.enabled:
                    continue
                source = resolve_cost_source(
                    db,
                    product_id=product_id,
                    inventory_first=policy.inventory_first,
                )
                if source is None:
                    try:
                        suspend_product_without_cost_source(
                            db,
                            product=product,
                            policy=policy,
                        )
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
                    continue
                waiting_runs = db.scalars(
                    select(DumpingRun)
                    .where(
                        DumpingRun.product_id == product_id,
                        DumpingRun.status == "awaiting_supplier_refresh",
                    )
                    .with_for_update()
                ).all()
                for waiting_run in waiting_runs:
                    waiting_run.status = "supplier_refresh_ready"
                db.commit()
            enqueue_competitor_scan(product_id, reason="supplier_snapshot_changed")
