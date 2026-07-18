from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .lease_engine import LeaseClaim, utc_now
from .monitoring import MonitorStatus, MonitorTarget
from .scheduler_engine import AdapterRegistry, ScheduledTaskResult, process_claimed_target
from .supplier_adapters.ozon_http import OzonHttpAdapter
from .suppliers import ProductBinding, Supplier, SupplierProduct


class MonitorTargetCreate(BaseModel):
    product_binding_id: int = Field(gt=0)
    interval_seconds: int = Field(default=300, ge=60, le=86_400)
    shard: int = Field(default=0, ge=0, le=99)


class MonitorTargetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_binding_id: int
    status: str
    interval_seconds: int
    next_check_at: object
    last_checked_at: object | None
    consecutive_failures: int
    lease_owner: str | None
    lease_until: object | None
    shard: int


class ManualRunRead(BaseModel):
    target_id: int
    status: str
    outcome: str | None
    changed: bool | None = None
    error: str | None = None


router = APIRouter(
    prefix="/api/monitor-targets",
    tags=["monitoring"],
    dependencies=[Depends(require_service_token)],
)


def _runtime_registry() -> AdapterRegistry:
    return AdapterRegistry({"ozon": OzonHttpAdapter()})


@router.post("", response_model=MonitorTargetRead, status_code=status.HTTP_201_CREATED)
def create_monitor_target(payload: MonitorTargetCreate, db: Session = Depends(get_db)):
    binding = db.get(ProductBinding, payload.product_binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="Product binding not found")
    if binding.status not in {"confirmed", "active", "degraded"}:
        raise HTTPException(
            status_code=409,
            detail="Binding must be confirmed or active before monitoring",
        )

    existing = db.scalar(
        select(MonitorTarget).where(MonitorTarget.product_binding_id == payload.product_binding_id)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Monitor target already exists for this binding")

    target = MonitorTarget(
        product_binding_id=payload.product_binding_id,
        status=MonitorStatus.ACTIVE.value,
        interval_seconds=payload.interval_seconds,
        next_check_at=utc_now(),
        shard=payload.shard,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.get("", response_model=list[MonitorTargetRead])
def list_monitor_targets(db: Session = Depends(get_db)):
    return list(db.scalars(select(MonitorTarget).order_by(MonitorTarget.id)).all())


def _claim_selected_target(db: Session, target_id: int, *, lease_seconds: int = 120) -> LeaseClaim:
    now = utc_now()
    target = db.scalar(
        select(MonitorTarget).where(MonitorTarget.id == target_id).with_for_update()
    )
    if target is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Monitor target not found")
    if target.status != MonitorStatus.ACTIVE.value:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Monitor target is {target.status}")
    if target.lease_until is not None and target.lease_until >= now:
        db.rollback()
        raise HTTPException(status_code=409, detail="Monitor target is already leased")

    context = db.execute(
        select(Supplier.code, SupplierProduct.url)
        .join(SupplierProduct, SupplierProduct.supplier_id == Supplier.id)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .where(ProductBinding.id == target.product_binding_id)
    ).one_or_none()
    if context is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="Monitor target supplier context is missing")
    if context[0].strip().lower() != "ozon":
        db.rollback()
        raise HTTPException(status_code=409, detail="Manual runtime currently supports supplier code 'ozon'")

    token = secrets.token_urlsafe(32)
    target.lease_owner = "manual-api"
    target.lease_token = token
    target.lease_until = now + timedelta(seconds=lease_seconds)
    db.commit()
    return LeaseClaim(
        target_id=target.id,
        product_binding_id=target.product_binding_id,
        lease_owner="manual-api",
        lease_token=token,
        lease_until=target.lease_until,
    )


@router.post("/{target_id}/run-now", response_model=ManualRunRead)
async def run_monitor_target_now(target_id: int, db: Session = Depends(get_db)):
    claim = _claim_selected_target(db, target_id)
    result: ScheduledTaskResult = await process_claimed_target(
        claim,
        registry=_runtime_registry(),
    )
    return ManualRunRead(
        target_id=result.target_id,
        status=result.status,
        outcome=result.outcome.value if result.outcome is not None else None,
        changed=result.changed,
        error=result.error,
    )
