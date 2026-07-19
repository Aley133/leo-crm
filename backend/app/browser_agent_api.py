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
from .browser_agent_ingestion import BrowserAgentResultError, persist_browser_agent_success
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .lease_engine import utc_now


class BrowserAgentJobCreate(BaseModel):
    supplier_product_id: int = Field(gt=0)
    url: str = Field(min_length=8, max_length=4000)
    monitor_target_id: int | None = Field(default=None, gt=0)


class BrowserAgentClaim(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=120, ge=30, le=600)


class BrowserAgentDispatch(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    supplier_code: str = Field(default="ozon", min_length=1, max_length=64)


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


@router.post("/dispatch-due")
def dispatch_due_jobs(payload: BrowserAgentDispatch, db: Session = Depends(get_db)):
    try:
        result = dispatch_due_browser_targets(
            db,
            limit=payload.limit,
            supplier_code=payload.supplier_code,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "queued_count": result.queued_count,
        "job_ids": list(result.queued_job_ids),
    }


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
    return {"id": job.id, "status": job.status}


@router.post("/claim")
def claim_browser_agent_job(payload: BrowserAgentClaim, db: Session = Depends(get_db)):
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
        return {"job": None}

    token = secrets.token_hex(24)
    job.status = BrowserAgentJobStatus.LEASED.value
    job.lease_owner = payload.agent_id
    job.lease_token = token
    job.lease_until = now + timedelta(seconds=payload.lease_seconds)
    db.commit()
    return {
        "job": {
            "id": job.id,
            "monitor_target_id": job.monitor_target_id,
            "supplier_product_id": job.supplier_product_id,
            "url": job.url,
            "lease_token": token,
            "lease_until": job.lease_until,
        }
    }


@router.post("/jobs/{job_id}/complete")
def complete_browser_agent_job(
    job_id: int,
    payload: BrowserAgentResult,
    db: Session = Depends(get_db),
):
    job = db.scalar(
        select(BrowserAgentJob).where(BrowserAgentJob.id == job_id).with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    if job.status != BrowserAgentJobStatus.LEASED.value or job.lease_token != payload.lease_token:
        raise HTTPException(status_code=409, detail="Browser agent lease is no longer valid")

    now = utc_now()
    if job.lease_until is None or job.lease_until < now:
        raise HTTPException(status_code=409, detail="Browser agent lease expired")

    succeeded = payload.status == BrowserAgentJobStatus.SUCCEEDED.value
    if not succeeded and payload.status != BrowserAgentJobStatus.FAILED.value:
        raise HTTPException(status_code=422, detail="status must be succeeded or failed")
    if succeeded and payload.payload is None:
        raise HTTPException(status_code=422, detail="successful browser agent result requires payload")

    attempt_id: int | None = None
    changed: bool | None = None
    try:
        if succeeded and job.monitor_target_id is not None:
            attempt_id, changed = persist_browser_agent_success(
                db,
                job=job,
                payload=payload.payload or {},
                finished_at=now,
            )

        job.status = payload.status
        job.result_payload = (
            json.dumps(payload.payload, ensure_ascii=False, sort_keys=True)
            if payload.payload is not None
            else None
        )
        job.error_code = payload.error_code
        job.error_message = payload.error_message
        job.finished_at = now
        job.lease_owner = None
        job.lease_token = None
        job.lease_until = None
        db.commit()
    except BrowserAgentResultError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return {
        "id": job.id,
        "status": job.status,
        "monitor_attempt_id": attempt_id,
        "changed": changed,
    }


@router.get("/jobs/{job_id}")
def read_browser_agent_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BrowserAgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    return {
        "id": job.id,
        "monitor_target_id": job.monitor_target_id,
        "supplier_product_id": job.supplier_product_id,
        "url": job.url,
        "status": job.status,
        "result": json.loads(job.result_payload) if job.result_payload else None,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }
