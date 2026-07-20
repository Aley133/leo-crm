from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .lease_engine import utc_now
from .models import Product
from .monitoring import MonitorAttempt, MonitorTarget, SourceHealth
from .suppliers import ProductBinding, Supplier, SupplierProduct


class MonitoringSummary(BaseModel):
    targets_total: int
    targets_active: int
    targets_with_failures: int
    jobs_queued: int
    jobs_leased: int
    jobs_failed: int
    attempts_total: int
    attempts_failed: int
    unhealthy_sources: int


class MonitoringJobRow(BaseModel):
    id: int
    status: str
    lifecycle_state: str
    wait_reason: str | None
    monitor_target_id: int | None
    product_id: int | None
    kaspi_product_id: str | None
    product_name: str | None
    supplier_code: str | None
    supplier_name: str | None
    supplier_product_url: str
    lease_owner: str | None
    lease_until: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class MonitoringAttemptRow(BaseModel):
    id: int
    target_id: int
    product_id: int | None
    kaspi_product_id: str | None
    product_name: str | None
    supplier_code: str | None
    outcome: str
    adapter_code: str
    access_strategy: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    http_status: int | None
    error_code: str | None
    error_message: str | None


class SourceHealthRow(BaseModel):
    supplier_id: int
    supplier_code: str
    supplier_name: str
    access_strategy: str
    status: str
    consecutive_failures: int
    blocked_until: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_code: str | None
    updated_at: datetime


class RuntimeEventRow(BaseModel):
    event: str
    occurred_at: datetime
    detail: str | None = None


router = APIRouter(
    prefix="/api/monitoring-center",
    tags=["monitoring-center"],
    dependencies=[Depends(require_service_token)],
)


def _get_job_for_update(db: Session, job_id: int) -> BrowserAgentJob:
    job = db.scalar(
        select(BrowserAgentJob)
        .where(BrowserAgentJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    return job


def _job_lifecycle(job: BrowserAgentJob) -> tuple[str, str | None]:
    if job.status == BrowserAgentJobStatus.QUEUED.value:
        return "waiting_for_agent", "Ожидает свободный Browser Agent и получение lease"
    if job.status == BrowserAgentJobStatus.LEASED.value:
        if job.lease_until is not None and job.lease_until < utc_now():
            return "lease_expired", "Lease истёк; задание может быть повторно забрано агентом"
        return "processing", f"Выполняется агентом {job.lease_owner or 'unknown'}"
    if job.status == BrowserAgentJobStatus.SUCCEEDED.value:
        return "finished", None
    if job.error_code == "operator_cancelled":
        return "cancelled", "Отменено оператором до получения lease"
    return "failed", job.error_code or "Завершено с ошибкой"


@router.get("/summary", response_model=MonitoringSummary)
def get_monitoring_summary(db: Session = Depends(get_db)) -> MonitoringSummary:
    targets_total = db.scalar(select(func.count()).select_from(MonitorTarget)) or 0
    targets_active = db.scalar(select(func.count()).select_from(MonitorTarget).where(MonitorTarget.status == "active")) or 0
    targets_with_failures = db.scalar(select(func.count()).select_from(MonitorTarget).where(MonitorTarget.consecutive_failures > 0)) or 0
    jobs_queued = db.scalar(select(func.count()).select_from(BrowserAgentJob).where(BrowserAgentJob.status == BrowserAgentJobStatus.QUEUED.value)) or 0
    jobs_leased = db.scalar(select(func.count()).select_from(BrowserAgentJob).where(BrowserAgentJob.status == BrowserAgentJobStatus.LEASED.value)) or 0
    jobs_failed = db.scalar(select(func.count()).select_from(BrowserAgentJob).where(BrowserAgentJob.status == BrowserAgentJobStatus.FAILED.value)) or 0
    attempts_total = db.scalar(select(func.count()).select_from(MonitorAttempt)) or 0
    attempts_failed = db.scalar(select(func.count()).select_from(MonitorAttempt).where(MonitorAttempt.error_code.is_not(None))) or 0
    unhealthy_sources = db.scalar(select(func.count()).select_from(SourceHealth).where(SourceHealth.status != "healthy")) or 0
    return MonitoringSummary(
        targets_total=targets_total,
        targets_active=targets_active,
        targets_with_failures=targets_with_failures,
        jobs_queued=jobs_queued,
        jobs_leased=jobs_leased,
        jobs_failed=jobs_failed,
        attempts_total=attempts_total,
        attempts_failed=attempts_failed,
        unhealthy_sources=unhealthy_sources,
    )


@router.get("/jobs", response_model=list[MonitoringJobRow])
def list_monitoring_jobs(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[MonitoringJobRow]:
    stmt = (
        select(
            BrowserAgentJob,
            Product.id,
            Product.kaspi_product_id,
            Product.name,
            Supplier.code,
            Supplier.name,
        )
        .outerjoin(MonitorTarget, MonitorTarget.id == BrowserAgentJob.monitor_target_id)
        .outerjoin(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .outerjoin(Product, Product.id == ProductBinding.product_id)
        .outerjoin(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .outerjoin(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .order_by(BrowserAgentJob.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(BrowserAgentJob.status == status)
    rows = db.execute(stmt).all()
    result: list[MonitoringJobRow] = []
    for job, product_id, kaspi_id, product_name, supplier_code, supplier_name in rows:
        lifecycle_state, wait_reason = _job_lifecycle(job)
        result.append(MonitoringJobRow(
            id=job.id,
            status=job.status,
            lifecycle_state=lifecycle_state,
            wait_reason=wait_reason,
            monitor_target_id=job.monitor_target_id,
            product_id=product_id,
            kaspi_product_id=kaspi_id,
            product_name=product_name,
            supplier_code=supplier_code,
            supplier_name=supplier_name,
            supplier_product_url=job.url,
            lease_owner=job.lease_owner,
            lease_until=job.lease_until,
            error_code=job.error_code,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            finished_at=job.finished_at,
        ))
    return result


@router.get("/jobs/{job_id}")
def inspect_monitoring_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(BrowserAgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    lifecycle_state, wait_reason = _job_lifecycle(job)
    return {
        "id": job.id,
        "status": job.status,
        "lifecycle_state": lifecycle_state,
        "wait_reason": wait_reason,
        "monitor_target_id": job.monitor_target_id,
        "supplier_product_id": job.supplier_product_id,
        "url": job.url,
        "lease_owner": job.lease_owner,
        "lease_until": job.lease_until,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }


@router.get("/jobs/{job_id}/events", response_model=list[RuntimeEventRow])
def list_monitoring_job_events(job_id: int, db: Session = Depends(get_db)) -> list[RuntimeEventRow]:
    job = db.get(BrowserAgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Browser agent job not found")
    lifecycle_state, wait_reason = _job_lifecycle(job)
    events = [RuntimeEventRow(event="created", occurred_at=job.created_at, detail="Job добавлен в очередь Browser Agent")]
    if job.status == BrowserAgentJobStatus.QUEUED.value:
        events.append(RuntimeEventRow(event="waiting_for_agent", occurred_at=job.updated_at, detail=wait_reason))
    if job.lease_owner and job.lease_until:
        events.append(RuntimeEventRow(event="lease_acquired", occurred_at=job.updated_at, detail=f"Lease owner: {job.lease_owner}; lease до {job.lease_until.isoformat()}"))
        events.append(RuntimeEventRow(event="processing", occurred_at=job.updated_at, detail="Browser Agent выполняет навигацию, парсинг и сохранение результата"))
    if job.finished_at:
        detail = job.error_message or job.error_code or job.status
        events.append(RuntimeEventRow(event=lifecycle_state, occurred_at=job.finished_at, detail=detail))
    return sorted(events, key=lambda item: item.occurred_at)


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_201_CREATED)
def retry_monitoring_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    original = _get_job_for_update(db, job_id)
    if original.status in (BrowserAgentJobStatus.QUEUED.value, BrowserAgentJobStatus.LEASED.value):
        raise HTTPException(status_code=409, detail="Only completed jobs can be retried")
    retry = BrowserAgentJob(
        monitor_target_id=original.monitor_target_id,
        supplier_product_id=original.supplier_product_id,
        url=original.url,
        status=BrowserAgentJobStatus.QUEUED.value,
    )
    db.add(retry)
    db.commit()
    db.refresh(retry)
    return {"id": retry.id, "status": retry.status, "retried_from_job_id": original.id}


@router.post("/jobs/{job_id}/cancel")
def cancel_monitoring_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = _get_job_for_update(db, job_id)
    if job.status != BrowserAgentJobStatus.QUEUED.value:
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled")
    now = utc_now()
    job.status = BrowserAgentJobStatus.FAILED.value
    job.error_code = "operator_cancelled"
    job.error_message = "Cancelled by CRM operator before lease acquisition"
    job.finished_at = now
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    db.commit()
    return {"id": job.id, "status": job.status, "error_code": job.error_code}


@router.get("/attempts", response_model=list[MonitoringAttemptRow])
def list_monitoring_attempts(
    only_errors: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[MonitoringAttemptRow]:
    stmt = (
        select(MonitorAttempt, Product.id, Product.kaspi_product_id, Product.name, Supplier.code)
        .join(MonitorTarget, MonitorTarget.id == MonitorAttempt.monitor_target_id)
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(Product, Product.id == ProductBinding.product_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .order_by(MonitorAttempt.id.desc())
        .limit(limit)
    )
    if only_errors:
        stmt = stmt.where(MonitorAttempt.error_code.is_not(None))
    rows = db.execute(stmt).all()
    return [MonitoringAttemptRow(
        id=attempt.id,
        target_id=attempt.monitor_target_id,
        product_id=product_id,
        kaspi_product_id=kaspi_id,
        product_name=product_name,
        supplier_code=supplier_code,
        outcome=attempt.outcome,
        adapter_code=attempt.adapter_code,
        access_strategy=attempt.access_strategy,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        duration_ms=attempt.duration_ms,
        http_status=attempt.http_status,
        error_code=attempt.error_code,
        error_message=attempt.error_message,
    ) for attempt, product_id, kaspi_id, product_name, supplier_code in rows]


@router.get("/sources", response_model=list[SourceHealthRow])
def list_source_health(db: Session = Depends(get_db)) -> list[SourceHealthRow]:
    rows = db.execute(
        select(SourceHealth, Supplier.code, Supplier.name)
        .join(Supplier, Supplier.id == SourceHealth.supplier_id)
        .order_by(SourceHealth.status, Supplier.code)
    ).all()
    return [SourceHealthRow(
        supplier_id=item.supplier_id,
        supplier_code=code,
        supplier_name=name,
        access_strategy=item.access_strategy,
        status=item.status,
        consecutive_failures=item.consecutive_failures,
        blocked_until=item.blocked_until,
        last_success_at=item.last_success_at,
        last_failure_at=item.last_failure_at,
        last_error_code=item.last_error_code,
        updated_at=item.updated_at,
    ) for item, code, name in rows]
