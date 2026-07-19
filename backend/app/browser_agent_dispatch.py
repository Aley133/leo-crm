from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .lease_engine import utc_now
from .monitoring import MonitorStatus, MonitorTarget
from .suppliers import ProductBinding, Supplier, SupplierProduct


@dataclass(frozen=True, slots=True)
class BrowserDispatchResult:
    queued_job_ids: tuple[int, ...]

    @property
    def queued_count(self) -> int:
        return len(self.queued_job_ids)


class BrowserQueueFailure(StrEnum):
    NOT_FOUND = "not_found"
    NOT_ACTIVE = "not_active"
    UNSUPPORTED_SUPPLIER = "unsupported_supplier"
    ALREADY_PENDING = "already_pending"


@dataclass(frozen=True, slots=True)
class BrowserQueueResult:
    job_id: int | None = None
    failure: BrowserQueueFailure | None = None


_ACTIVE_JOB_STATUSES = (
    BrowserAgentJobStatus.QUEUED.value,
    BrowserAgentJobStatus.LEASED.value,
)


def build_due_browser_targets_statement(*, limit: int, supplier_code: str):
    """Build the PostgreSQL-safe due-target claim statement.

    MonitorTarget is the only scheduling source of truth. Existing queued or leased
    browser jobs are excluded through a scalar subquery rather than a correlated
    EXISTS expression, keeping the row-locking query predictable on PostgreSQL.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    normalized_supplier = supplier_code.strip().casefold()
    if not normalized_supplier:
        raise ValueError("supplier_code must not be empty")

    active_job_target_ids = (
        select(BrowserAgentJob.monitor_target_id)
        .where(
            BrowserAgentJob.monitor_target_id.is_not(None),
            BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES),
        )
    )

    return (
        select(MonitorTarget, SupplierProduct.id, SupplierProduct.url)
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(
            MonitorTarget.status == MonitorStatus.ACTIVE.value,
            MonitorTarget.next_check_at <= utc_now(),
            Supplier.code == normalized_supplier,
            MonitorTarget.id.not_in(active_job_target_ids),
        )
        .order_by(MonitorTarget.next_check_at, MonitorTarget.id)
        .with_for_update(of=MonitorTarget, skip_locked=True)
        .limit(limit)
    )


def queue_browser_target_now(
    session: Session,
    *,
    target_id: int,
    supplier_code: str = "ozon",
) -> BrowserQueueResult:
    """Queue one selected active target without changing its monitoring cadence.

    The target row is locked so concurrent manual requests cannot enqueue duplicate
    jobs. This function owns no transaction and never commits.
    """
    normalized_supplier = supplier_code.strip().casefold()
    row = session.execute(
        select(MonitorTarget, SupplierProduct.id, SupplierProduct.url, Supplier.code)
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(MonitorTarget.id == target_id)
        .with_for_update(of=MonitorTarget)
    ).one_or_none()
    if row is None:
        return BrowserQueueResult(failure=BrowserQueueFailure.NOT_FOUND)

    target, supplier_product_id, url, actual_supplier_code = row
    if target.status != MonitorStatus.ACTIVE.value:
        return BrowserQueueResult(failure=BrowserQueueFailure.NOT_ACTIVE)
    if actual_supplier_code.strip().casefold() != normalized_supplier:
        return BrowserQueueResult(failure=BrowserQueueFailure.UNSUPPORTED_SUPPLIER)

    pending_job_id = session.scalar(
        select(BrowserAgentJob.id)
        .where(
            BrowserAgentJob.monitor_target_id == target.id,
            BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES),
        )
        .order_by(BrowserAgentJob.id)
        .limit(1)
    )
    if pending_job_id is not None:
        return BrowserQueueResult(
            job_id=pending_job_id,
            failure=BrowserQueueFailure.ALREADY_PENDING,
        )

    job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=supplier_product_id,
        url=url,
        status=BrowserAgentJobStatus.QUEUED.value,
    )
    session.add(job)
    session.flush()
    return BrowserQueueResult(job_id=job.id)


def dispatch_due_browser_targets(
    session: Session,
    *,
    limit: int = 100,
    supplier_code: str = "ozon",
) -> BrowserDispatchResult:
    """Queue independently due monitor targets without committing.

    A target is eligible only when it is active, its own next_check_at has arrived,
    and no queued or leased job exists for that exact target. Target rows are locked
    with SKIP LOCKED so multiple dispatchers can run safely without duplicate work.
    """
    rows = session.execute(
        build_due_browser_targets_statement(limit=limit, supplier_code=supplier_code)
    ).all()

    jobs: list[BrowserAgentJob] = []
    for target, supplier_product_id, url in rows:
        job = BrowserAgentJob(
            monitor_target_id=target.id,
            supplier_product_id=supplier_product_id,
            url=url,
            status=BrowserAgentJobStatus.QUEUED.value,
        )
        session.add(job)
        jobs.append(job)

    session.flush()
    return BrowserDispatchResult(tuple(job.id for job in jobs))