from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .monitoring import MonitorTarget
from .suppliers import ProductBinding, Supplier, SupplierProduct


router = APIRouter(
    prefix="/api/monitor-targets",
    tags=["browser-agent"],
    dependencies=[Depends(require_service_token)],
)


@router.post("/{target_id}/queue-browser-agent", status_code=status.HTTP_201_CREATED)
def queue_monitor_target_for_browser_agent(target_id: int, db: Session = Depends(get_db)):
    context = db.execute(
        select(SupplierProduct.id, SupplierProduct.url, Supplier.code)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .join(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(MonitorTarget.id == target_id)
    ).one_or_none()
    if context is None:
        raise HTTPException(status_code=404, detail="Monitor target or supplier product not found")
    supplier_product_id, url, supplier_code = context
    if str(supplier_code).strip().casefold() != "ozon":
        raise HTTPException(status_code=409, detail="Browser agent currently supports supplier code 'ozon'")

    pending = db.scalar(
        select(BrowserAgentJob)
        .where(
            BrowserAgentJob.monitor_target_id == target_id,
            BrowserAgentJob.status.in_(
                [BrowserAgentJobStatus.QUEUED.value, BrowserAgentJobStatus.LEASED.value]
            ),
        )
        .order_by(BrowserAgentJob.id.desc())
        .limit(1)
    )
    if pending is not None:
        return {"job_id": pending.id, "status": pending.status, "reused": True}

    job = BrowserAgentJob(
        monitor_target_id=target_id,
        supplier_product_id=supplier_product_id,
        url=url,
        status=BrowserAgentJobStatus.QUEUED.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": job.id, "status": job.status, "reused": False}
