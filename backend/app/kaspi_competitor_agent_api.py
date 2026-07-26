from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .dumping_runner import apply_competitor_snapshot
from .kaspi_offer_competitor import KaspiCompetitorSnapshot
from .models import Product

router = APIRouter(
    prefix="/api/kaspi-competitor-agent",
    tags=["kaspi-competitor-agent"],
    dependencies=[Depends(require_service_token)],
)

LEASE_SECONDS = 180


class AgentClaim(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=32)


class AgentComplete(BaseModel):
    lease_token: str = Field(min_length=16, max_length=128)
    status: str
    own_price_kzt: Decimal | None = None
    competitor_price_kzt: Decimal | None = None
    competitor_name: str | None = Field(default=None, max_length=500)
    own_position: int | None = Field(default=None, ge=1)
    seller_count: int = Field(default=0, ge=0)
    product_url: str | None = Field(default=None, max_length=4000)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=4000)


def _now() -> datetime:
    return datetime.now(UTC)


def queue_competitor_job(db: Session, *, product_id: int, reason: str) -> DumpingRun:
    existing = db.scalar(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == product_id,
            DumpingRun.status.in_(("queued_local", "leased_local")),
        )
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise ValueError("Демпинг для товара не подключён")
    job = DumpingRun(
        product_id=product_id,
        dumping_policy_id=policy.id,
        status="queued_local",
        published=False,
        explanation_json={"reason": reason, "agent_type": "kaspi_competitor"},
    )
    db.add(job)
    db.flush()
    return job


def state_for_product(db: Session, product_id: int) -> dict | None:
    job = db.scalar(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == product_id,
            DumpingRun.status.in_(("queued_local", "leased_local", "failed_local", "succeeded_local")),
        )
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    if job is None:
        return None
    meta = job.explanation_json or {}
    return {
        "job_id": job.id,
        "status": job.status,
        "stage": meta.get("stage") or job.status,
        "reason": meta.get("reason"),
        "agent_id": meta.get("agent_id"),
        "leased_at": meta.get("leased_at"),
        "lease_until": meta.get("lease_until"),
        "last_error": meta.get("error_message"),
        "updated_at": meta.get("updated_at") or job.created_at,
    }


@router.post("/claim")
def claim_job(payload: AgentClaim, db: Session = Depends(get_db)) -> dict:
    now = _now()
    job = db.scalar(
        select(DumpingRun)
        .where(
            or_(
                DumpingRun.status == "queued_local",
                (DumpingRun.status == "leased_local")
                & (DumpingRun.explanation_json["lease_until"].as_string() < now.isoformat()),
            )
        )
        .order_by(DumpingRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return {"job": None}

    product = db.get(Product, job.product_id)
    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == job.product_id))
    feed = db.scalar(
        select(KaspiXmlFeed).where(KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1)
    )
    if product is None or policy is None or feed is None or not feed.merchant_id:
        job.status = "failed_local"
        job.explanation_json = {**(job.explanation_json or {}), "error_message": "Product, policy or XML feed missing"}
        db.commit()
        return {"job": None}

    token = secrets.token_hex(24)
    lease_until = now + timedelta(seconds=LEASE_SECONDS)
    job.status = "leased_local"
    job.explanation_json = {
        **(job.explanation_json or {}),
        "agent_id": payload.agent_id,
        "hostname": payload.hostname,
        "platform": payload.platform,
        "version": payload.version,
        "lease_token": token,
        "leased_at": now.isoformat(),
        "lease_until": lease_until.isoformat(),
        "stage": "local_scan",
        "updated_at": now.isoformat(),
    }
    db.commit()
    return {
        "job": {
            "id": job.id,
            "product_id": product.id,
            "name": product.name,
            "brand": product.brand,
            "kaspi_product_id": product.kaspi_product_id,
            "merchant_sku": product.merchant_sku,
            "own_merchant_id": feed.merchant_id,
            "city_id": policy.city_id,
            "zone_id": policy.zone_id,
            "lease_token": token,
            "lease_until": lease_until,
        }
    }


@router.post("/jobs/{job_id}/complete")
def complete_job(job_id: int, payload: AgentComplete, db: Session = Depends(get_db)) -> dict:
    job = db.scalar(select(DumpingRun).where(DumpingRun.id == job_id).with_for_update())
    if job is None:
        raise HTTPException(status_code=404, detail="Competitor job not found")
    meta = job.explanation_json or {}
    if job.status != "leased_local" or meta.get("lease_token") != payload.lease_token:
        raise HTTPException(status_code=409, detail="Competitor job lease is no longer valid")

    if payload.status == "succeeded":
        market = KaspiCompetitorSnapshot(
            own_price_kzt=payload.own_price_kzt,
            competitor_price_kzt=payload.competitor_price_kzt,
            competitor_name=payload.competitor_name,
            own_position=payload.own_position,
            seller_count=payload.seller_count,
            product_url=payload.product_url or "",
        )
        result = apply_competitor_snapshot(db, product_id=job.product_id, market=market)
        job.status = "succeeded_local"
        job.explanation_json = {
            **meta,
            "stage": "completed",
            "updated_at": _now().isoformat(),
            "result": result,
        }
    elif payload.status == "failed":
        job.status = "failed_local"
        job.explanation_json = {
            **meta,
            "stage": "failed",
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "updated_at": _now().isoformat(),
        }
        db.commit()
        return {"id": job.id, "status": job.status}
    else:
        raise HTTPException(status_code=422, detail="status must be succeeded or failed")

    db.commit()
    return {"id": job.id, "status": job.status, "result": job.explanation_json.get("result")}
