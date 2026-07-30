from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
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
AGENT_ONLINE_SECONDS = 45
LEASE_RECOVERY_SCAN_LIMIT = 100
_AGENT_HEARTBEATS: dict[str, dict] = {}
_AGENT_HEARTBEATS_LOCK = Lock()


class AgentClaim(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=32)
    concurrency: int | None = Field(default=None, ge=1, le=32)


class AgentHeartbeat(AgentClaim):
    status: str = Field(default="online", max_length=32)


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


def _touch_agent(payload: AgentClaim, *, status: str = "online") -> dict:
    now = _now()
    record = {
        "agent_id": payload.agent_id,
        "hostname": payload.hostname,
        "platform": payload.platform,
        "version": payload.version,
        "concurrency": payload.concurrency,
        "status": status,
        "last_seen_at": now.isoformat(),
    }
    with _AGENT_HEARTBEATS_LOCK:
        _AGENT_HEARTBEATS[payload.agent_id] = record
    return record


def _agent_status_payload() -> dict:
    now = _now()
    with _AGENT_HEARTBEATS_LOCK:
        agents = [dict(item) for item in _AGENT_HEARTBEATS.values()]
    for item in agents:
        try:
            seen = datetime.fromisoformat(str(item["last_seen_at"]))
            item["online"] = (now - seen).total_seconds() <= AGENT_ONLINE_SECONDS
        except (TypeError, ValueError):
            item["online"] = False
    agents.sort(key=lambda item: str(item.get("last_seen_at") or ""), reverse=True)
    return {
        "online": any(item["online"] for item in agents),
        "online_count": sum(1 for item in agents if item["online"]),
        "agents": agents,
        "checked_at": now.isoformat(),
    }


def _metadata_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


def _json_compatible(value: object) -> object:
    """Convert exact domain values before persisting them in a JSON column."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _claimable_job(db: Session, *, now: datetime) -> tuple[DumpingRun | None, bool]:
    queued = db.scalar(
        select(DumpingRun)
        .where(DumpingRun.status == "queued_local")
        .order_by(DumpingRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if queued is not None:
        return queued, False

    leased = db.scalars(
        select(DumpingRun)
        .where(DumpingRun.status == "leased_local")
        .order_by(DumpingRun.id)
        .with_for_update(skip_locked=True)
        .limit(LEASE_RECOVERY_SCAN_LIMIT)
    ).all()
    for candidate in leased:
        lease_until = _metadata_datetime((candidate.explanation_json or {}).get("lease_until"))
        if lease_until is not None and lease_until < now:
            return candidate, True
    return None, False


def queue_competitor_job(db: Session, *, product_id: int, reason: str) -> DumpingRun:
    policy = db.scalar(
        select(DumpingPolicy)
        .where(DumpingPolicy.product_id == product_id)
        .with_for_update()
    )
    if policy is None or not policy.enabled:
        raise ValueError("Демпинг для товара не подключён")

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


@router.post("/heartbeat")
def heartbeat(payload: AgentHeartbeat) -> dict:
    record = _touch_agent(payload, status=payload.status)
    return {"accepted": True, "agent": record}


@router.get("/agents/status")
def read_agent_status() -> dict:
    return _agent_status_payload()


@router.post("/claim")
def claim_job(payload: AgentClaim, db: Session = Depends(get_db)) -> dict:
    _touch_agent(payload)
    now = _now()
    job, recovered = _claimable_job(db, now=now)
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
    previous_meta = job.explanation_json or {}
    lease_attempt = int(previous_meta.get("lease_attempt") or 0) + 1
    job.status = "leased_local"
    job.explanation_json = {
        **previous_meta,
        "agent_id": payload.agent_id,
        "hostname": payload.hostname,
        "platform": payload.platform,
        "version": payload.version,
        "lease_token": token,
        "lease_attempt": lease_attempt,
        "leased_at": now.isoformat(),
        "lease_until": lease_until.isoformat(),
        "stage": "local_scan",
        "updated_at": now.isoformat(),
        **(
            {
                "lease_recovered_at": now.isoformat(),
                "previous_lease_until": previous_meta.get("lease_until"),
            }
            if recovered
            else {}
        ),
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
        try:
            with db.begin_nested():
                result = apply_competitor_snapshot(
                    db,
                    product_id=job.product_id,
                    market=market,
                )
        except ValueError as exc:
            job.status = "failed_local"
            job.explanation_json = {
                **meta,
                "stage": "failed",
                "error_code": "dumping_decision_rejected",
                "error_message": str(exc),
                "updated_at": _now().isoformat(),
            }
            db.commit()
            return {"id": job.id, "status": job.status}

        persisted_result = _json_compatible(result)
        job.status = "succeeded_local"
        job.explanation_json = {
            **meta,
            "stage": "completed",
            "updated_at": _now().isoformat(),
            "result": persisted_result,
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
