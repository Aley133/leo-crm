from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .models import Product
from .monitoring import MonitorStatus, MonitorTarget, SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ProductMetrics(BaseModel):
    total: int
    bound: int
    without_supplier: int


class MonitoringMetrics(BaseModel):
    active: int
    degraded: int
    errors: int
    stale: int
    queued_jobs: int
    leased_jobs: int


class SupplierMetrics(BaseModel):
    sources: int
    offers: int
    offers_with_state: int
    available: int
    unavailable: int


class DashboardSummary(BaseModel):
    generated_at: datetime
    stale_after_minutes: int
    products: ProductMetrics
    monitoring: MonitoringMetrics
    suppliers: SupplierMetrics


router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_service_token)],
)


@router.get("", response_model=DashboardSummary)
def get_dashboard_summary(
    stale_after_minutes: int = Query(default=30, ge=5, le=10_080),
    db: Session = Depends(get_db),
) -> DashboardSummary:
    generated_at = datetime.now(UTC)
    stale_before = generated_at - timedelta(minutes=stale_after_minutes)

    total_products = db.scalar(select(func.count()).select_from(Product)) or 0
    bound_products = db.scalar(
        select(func.count(func.distinct(ProductBinding.product_id))).select_from(ProductBinding)
    ) or 0

    active_targets = db.scalar(
        select(func.count())
        .select_from(MonitorTarget)
        .where(MonitorTarget.status == MonitorStatus.ACTIVE.value)
    ) or 0
    degraded_targets = db.scalar(
        select(func.count())
        .select_from(MonitorTarget)
        .where(
            MonitorTarget.status.in_(
                [MonitorStatus.DEGRADED.value, MonitorStatus.MANUAL_REVIEW.value]
            )
        )
    ) or 0
    failed_targets = db.scalar(
        select(func.count())
        .select_from(MonitorTarget)
        .where(MonitorTarget.consecutive_failures > 0)
    ) or 0
    stale_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.last_checked_at < stale_before)
    ) or 0
    queued_jobs = db.scalar(
        select(func.count())
        .select_from(BrowserAgentJob)
        .where(BrowserAgentJob.status == BrowserAgentJobStatus.QUEUED.value)
    ) or 0
    leased_jobs = db.scalar(
        select(func.count())
        .select_from(BrowserAgentJob)
        .where(BrowserAgentJob.status == BrowserAgentJobStatus.LEASED.value)
    ) or 0

    supplier_sources = db.scalar(
        select(func.count()).select_from(Supplier).where(Supplier.is_active.is_(True))
    ) or 0
    supplier_offers = db.scalar(select(func.count()).select_from(SupplierProduct)) or 0
    offers_with_state = db.scalar(select(func.count()).select_from(SupplierOfferState)) or 0
    available_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.available.is_(True))
    ) or 0
    unavailable_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.available.is_(False))
    ) or 0

    return DashboardSummary(
        generated_at=generated_at,
        stale_after_minutes=stale_after_minutes,
        products=ProductMetrics(
            total=total_products,
            bound=bound_products,
            without_supplier=max(0, total_products - bound_products),
        ),
        monitoring=MonitoringMetrics(
            active=active_targets,
            degraded=degraded_targets,
            errors=failed_targets,
            stale=stale_offers,
            queued_jobs=queued_jobs,
            leased_jobs=leased_jobs,
        ),
        suppliers=SupplierMetrics(
            sources=supplier_sources,
            offers=supplier_offers,
            offers_with_state=offers_with_state,
            available=available_offers,
            unavailable=unavailable_offers,
        ),
    )
