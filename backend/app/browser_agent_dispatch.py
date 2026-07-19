from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, select
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


def dispatch_due_browser_targets(
    session: Session,
    *,
    limit: int = 100,
    supplier_code: str = "ozon",
) -> BrowserDispatchResult:
    """Queue independently due monitor targets without committing.

    MonitorTarget remains the scheduling source of truth. A target is eligible only
    when it is active, its own next_check_at has arrived, and no queued/leased job
    already exists for that exact target. Target rows are locked with SKIP LOCKED so
    multiple dispatchers can run safely without producing duplicate work.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    normalized_supplier = supplier_code.strip().casefold()
    if not normalized_supplier:
        raise ValueError("supplier_code must not be empty")

    now = utc_now()
    pending_for_target = exists(
        select(BrowserAgentJob.id).where(
            BrowserAgentJob.monitor_target_id == MonitorTarget.id,
            BrowserAgentJob.status.in_(
                (BrowserAgentJobStatus.QUEUED.value, BrowserAgentJobStatus.LEASED.value)
            ),
        )
    )

    rows = session.execute(
        select(MonitorTarget, SupplierProduct.id, SupplierProduct.url)
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(
            MonitorTarget.status == MonitorStatus.ACTIVE.value,
            MonitorTarget.next_check_at <= now,
            Supplier.code == normalized_supplier,
            ~pending_for_target,
        )
        .order_by(MonitorTarget.next_check_at, MonitorTarget.id)
        .with_for_update(of=MonitorTarget, skip_locked=True)
        .limit(limit)
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
