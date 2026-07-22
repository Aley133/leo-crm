from __future__ import annotations

import json
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_dispatch import dispatch_due_browser_targets
from .browser_agent_failure import persist_browser_agent_failure
from .browser_agent_ingestion import BrowserAgentResultError, persist_browser_agent_success
from .browser_agent_job_contract import BrowserAgentJobType, decode_browser_agent_job, serialize_claim_payload
from .browser_agent_models import BrowserAgent, BrowserAgentJob, BrowserAgentJobStatus
from .browser_agent_presence import record_browser_agent_heartbeat
from .db import get_db
from .lease_engine import utc_now


class BrowserAgentJobCreate(BaseModel):
    job_type: BrowserAgentJobType = BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION
    supplier_product_id: int = Field(gt=0)
    url: str = Field(min_length=8, max_length=4000)
    monitor_target_id: int | None = Field(default=None, gt=0)


class BrowserAgentClaim(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=120, ge=30, le=600)
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=32)


class BrowserAgentDispatch(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    supplier_code: str = Field(default="ozon", min_length=1, max_length=64)


class BrowserAgentHeartbeat(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    status: str = Field(default="idle", min_length=1, max_length=32)
    version: str | None = Field(default=None, max_length=32)
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=128)
    current_job_id: int | None = Field(default=None, gt=0)


class BrowserAgentResult(BaseModel):
    lease_token: str = Field(min_length=16, max_length=128)
    status: str
    payload: dict | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=4000)


router = APIRouter(
    prefix="/api/browser-agent",
    tags=["browser-agent"],
    dependencies=[Depends(require_service_token)],
)


def _job_envelope(job: BrowserAgentJob):
    return decode_browser_agent_job(
        supplier_product_id=job.supplier_product_id,
        url=job.url,
        monitor_target_id=job.monitor_target_id,
    )


def _normalize_known_business_outcome(
    payload: BrowserAgentResult,
    *,
    job: BrowserAgentJob,
    observed_at,
) -> BrowserAgentResult:
    message = (payload.error_message or "").strip()
    host_is_wildberries = "wildberries.ru" in job.url.casefold() or "wb.ru" in job.url.casefold()
    if (
        payload.status == BrowserAgentJobStatus.FAILED.value
        and payload.error_code == "AdapterParseError"
        and host_is_wildberries
        and message == "Wildberries product is out of stock"
    ):
        return BrowserAgentResult(
            lease_token=payload.lease_token,
            status=BrowserAgentJobStatus.SUCCEEDED.value,
            payload={
                "price": None,
                "old_price": None,
                "currency": "KZT",
                "available": False,
                "stock": 0,
                "delivery_days": None,
                "seller": None,
                "adapter_schema_version": "wildberries-browser-verified-v5",
                "observed_at": observed_at.isoformat(),
                "raw_metadata": {
                    "source": "wb_browser_verified",
                    "business_state": "out_of_stock",
                    "normalized_from_legacy_error": True,
                },
            },
        )
    return payload


def _upsert_agent(
    db: Session,
    *,
    agent_id: str,
    status_value: str,
    current_job_id: int | None = None,
    hostname: str | None = None,
    platform: str | None = None,
    version: str | None = None,
) -> BrowserAgent:
    now = utc_now()
    agent = db.scalar(select(BrowserAgent).where(BrowserAgent.agent_id == agent_id).with_for_update())
    if agent is None:
        agent = BrowserAgent(
            agent_id=agent_id,
            hostname=hostname,
            platform=platform,
            version=version,
            status=status_value,
            current_job_id=current_job_id,
            last_seen_at=now,
        )
        db.add(agent)
    else:
        agent.status = status_value
        agent.current_job_id = current_job_id
        agent.last_seen_at = now
        if hostname:
            agent.hostname = hostname
        if platform:
            agent.platform = platform
        if version:
            agent.version = version
    record_browser_agent_heartbeat(
        agent_id=agent_id,
        status=status_value,
        version=version or agent.version,
        current_job_id=current_job_id,
    )
    return agent


@router.post("/heartbeat")
def heartbeat_browser_agent(payload: BrowserAgentHeartbeat, db: Session = Depends(get_db)):
    agent = _upsert_agent(
        db,
        agent_id=payload.agent_id,
        status_value=payload.status,
        version=payload.version,
        hostname=payload.hostname,
        platform=payload.platform,
        current_job_id=payload.current_job_id,
    )
    db.commit()
    db.refresh(agent)
    return {
        "agent_id": agent.agent_id,
        "hostname": agent.hostname,
        "platform": agent.platform,
        "status": agent.status,
        "version": agent.version,
        "current_job_id": agent.current_job_id,
        "last_seen_at": agent.last_seen_at,
    }


@router.get("/agents")
def list_browser_agents(db: Session = Depends(get_db)):
    cutoff = utc_now() - timedelta(seconds=30)
    agents = db.scalars(select(BrowserAgent).order_by(BrowserAgent.agent_id)).all()
    return [
        {
            "agent_id": item.agent_id,
            "hostname": item.hostname,
            "platform": item.platform,
            "status": item.status if item.last_seen_at >= cutoff else "offline",
            "version": item.version,
            "current_job_id": item.current_job_id,
            "last_seen_at": item.last_seen_at,
            "leases_taken": item.leases_taken,
            "leases_succeeded": item.leases_succeeded,
            "leases_failed": item.leases_failed,
        }
        for item in agents
    ]


@router.post("/dispatch-due")
def dispatch_due_jobs(payload: BrowserAgentDispatch, db: Session = Depends(get_db)):
    try:
        result = dispatch_due_browser_targets(db, limit=payload.limit, supplier_code=payload.supplier_code)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"queued_count": result.queued_count, "job_ids": list(result.queued_job_ids)}


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_browser_agent_job(payload: BrowserAgentJobCreate, db: Session = Depends(get_db)):
    job = BrowserAgentJob(
        monitor_target_id=payload.monitor_target_id,
        supplier_product_id=payload.supplier_product_id,
        url=payload.url,
        status=BrowserAgentJobStatus.QUEUED.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"id": job.id, "status": job.status, **serialize_claim_payload(_job_envelope(job))}


@router.post("/claim")
def claim_browser_agent_job(payload: BrowserAgentClaim, db: Session = Depends(get_db)):
    agent = _upsert_agent(
        db,
        agent_id=payload.agent_id,
        status_value="claiming",
        hostname=payload.hostname,
        platform=payload.platform,
        version=payload.version,
    )
    now = utc_now()
    job = db.scalar(
        select(BrowserAgentJob)
        .where(
            or_(
                BrowserAgentJob.status == BrowserAgentJobStatus.QUEUED.value,
                (BrowserAgentJob.status == BrowserAgentJobStatus.LEASED.value)
                & (BrowserAgentJob.lease_until < now),
            )
        )
        .order_by(BrowserAgentJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        agent.status = "idle"
        agent.current_job_id = None
        db.commit()
        record_browser_agent_heartbeat(agent_id=payload.agent_id, status="idle", version=agent.version)
        return {"job": None}

    token = secrets.token_hex(24)
    job.status = BrowserAgentJobStatus.LEASED.value
    job.lease_owner = payload.agent_id
    job.lease_token = token
    job.lease_until = now + timedelta(seconds=payload.lease_seconds)
    agent.status = "running"
    agent.current_job_id = job.id
    agent.leases_taken += 1
    db.commit()
    record_browser_agent_heartbeat(
        agent_id=payload.agent_id,
        status="running",
        version=agent.version,
        current_job_id=job.id,
    )
    envelope = _job_envelope(job)
    return {
        "job": {
            "id": job.id,
            "monitor_target_id": job.monitor_target_id,
            "supplier_product_id": job.supplier_product_id,
            "url": job.url,
            **serialize_claim_payload(envelope),
            "lease_token": token,
            "lease_until": job.lease_until,
        }
    }


@router.post("/jobs/{job_id}/complete")
def complete_browser_agent_job(job_id: int, payload: BrowserAgentResult, db: Session = Depends(get_db)):
    job = db.scalar(select(BrowserAgentJob).where(BrowserAgentJob.id == job_id).with_for_update())
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    if job.status != BrowserAgentJobStatus.LEASED.value or job.lease_token != payload.lease_token:
        raise HTTPException(status_code=409, detail="Browser agent lease is no longer valid")

    now = utc_now()
    if job.lease_until is None or job.lease_until < now:
        raise HTTPException(status_code=409, detail="Browser agent lease expired")

    payload = _normalize_known_business_outcome(payload, job=job, observed_at=now)
    succeeded = payload.status == BrowserAgentJobStatus.SUCCEEDED.value
    if not succeeded and payload.status != BrowserAgentJobStatus.FAILED.value:
        raise HTTPException(status_code=422, detail="status must be succeeded or failed")
    if succeeded and payload.payload is None:
        raise HTTPException(status_code=422, detail="successful browser agent result requires payload")

    agent_id = job.lease_owner
    attempt_id: int | None = None
    changed: bool | None = None
    try:
        if succeeded and job.monitor_target_id is not None:
            attempt_id, changed = persist_browser_agent_success(
                db, job=job, payload=payload.payload or {}, finished_at=now
            )
        elif not succeeded and job.monitor_target_id is not None:
            attempt_id = persist_browser_agent_failure(
                db,
                job=job,
                error_code=payload.error_code,
                error_message=payload.error_message,
                finished_at=now,
            )

        job.status = payload.status
        job.result_payload = json.dumps(payload.payload, ensure_ascii=False, sort_keys=True) if payload.payload is not None else None
        job.error_code = payload.error_code
        job.error_message = payload.error_message
        job.finished_at = now
        job.lease_owner = None
        job.lease_token = None
        job.lease_until = None

        if agent_id:
            agent = db.scalar(select(BrowserAgent).where(BrowserAgent.agent_id == agent_id).with_for_update())
            if agent:
                agent.status = "idle"
                agent.current_job_id = None
                agent.last_seen_at = now
                if succeeded:
                    agent.leases_succeeded += 1
                else:
                    agent.leases_failed += 1
        db.commit()
        if agent_id:
            record_browser_agent_heartbeat(agent_id=agent_id, status="idle")
    except BrowserAgentResultError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return {
        "id": job.id,
        "status": job.status,
        "job_type": BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION.value,
        "monitor_attempt_id": attempt_id,
        "changed": changed,
    }


@router.get("/jobs/{job_id}")
def read_browser_agent_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BrowserAgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    envelope = _job_envelope(job)
    return {
        "id": job.id,
        "monitor_target_id": job.monitor_target_id,
        "supplier_product_id": job.supplier_product_id,
        "url": job.url,
        **serialize_claim_payload(envelope),
        "status": job.status,
        "result": json.loads(job.result_payload) if job.result_payload else None,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
