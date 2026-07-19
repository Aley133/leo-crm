from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .lease_engine import ClaimFailure, LeaseClaim, claim_target, utc_now
from .monitoring import (
    MonitorAttempt,
    MonitorStatus,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from .scheduler_engine import AdapterRegistry, ScheduledTaskResult, process_claimed_target
from .supplier_adapters.ozon_browser import OzonBrowserAdapter
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
    next_check_at: datetime
    last_checked_at: datetime | None
    consecutive_failures: int
    lease_owner: str | None
    lease_until: datetime | None
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
    return AdapterRegistry({"ozon": OzonBrowserAdapter()})


async def _close_runtime_registry(registry: AdapterRegistry) -> None:
    adapter = registry.get("ozon")
    close = getattr(adapter, "close", None)
    if close is not None:
        await close()


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


def _validate_manual_runtime_context(db: Session, target_id: int) -> None:
    context = db.execute(
        select(Supplier.code, SupplierProduct.url)
        .join(SupplierProduct, SupplierProduct.supplier_id == Supplier.id)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .join(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .where(MonitorTarget.id == target_id)
    ).one_or_none()
    if context is None:
        raise HTTPException(status_code=404, detail="Monitor target or supplier context not found")
    if context[0].strip().lower() != "ozon":
        raise HTTPException(status_code=409, detail="Manual runtime currently supports supplier code 'ozon'")


def _claim_selected_target(db: Session, target_id: int, *, lease_seconds: int = 120) -> LeaseClaim:
    _validate_manual_runtime_context(db, target_id)
    result = claim_target(
        db,
        target_id=target_id,
        lease_owner="manual-api",
        lease_seconds=lease_seconds,
        require_due=False,
    )
    if result.claim is not None:
        return result.claim
    if result.failure is ClaimFailure.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Monitor target not found")
    if result.failure is ClaimFailure.NOT_ACTIVE:
        raise HTTPException(status_code=409, detail=f"Monitor target is {result.target_status}")
    if result.failure is ClaimFailure.ALREADY_LEASED:
        raise HTTPException(status_code=409, detail="Monitor target is already leased")
    raise HTTPException(status_code=409, detail=f"Monitor target cannot be claimed: {result.failure}")


@router.post("/{target_id}/run-now", response_model=ManualRunRead)
async def run_monitor_target_now(target_id: int, db: Session = Depends(get_db)):
    claim = _claim_selected_target(db, target_id)
    db.close()
    registry = _runtime_registry()
    try:
        result: ScheduledTaskResult = await process_claimed_target(
            claim,
            registry=registry,
        )
    finally:
        await _close_runtime_registry(registry)
    return ManualRunRead(
        target_id=result.target_id,
        status=result.status,
        outcome=result.outcome.value if result.outcome is not None else None,
        changed=result.changed,
        error=result.error,
    )


@router.get("/{target_id}/snapshot")
def get_monitor_target_snapshot(target_id: int, db: Session = Depends(get_db)):
    target = db.get(MonitorTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Monitor target not found")

    supplier_product_id = db.scalar(
        select(ProductBinding.supplier_product_id).where(
            ProductBinding.id == target.product_binding_id
        )
    )
    last_attempt = db.scalar(
        select(MonitorAttempt)
        .where(MonitorAttempt.monitor_target_id == target_id)
        .order_by(MonitorAttempt.id.desc())
        .limit(1)
    )
    state = None
    observation_count = 0
    if supplier_product_id is not None:
        state = db.scalar(
            select(SupplierOfferState).where(
                SupplierOfferState.supplier_product_id == supplier_product_id
            )
        )
        observation_count = db.scalar(
            select(func.count())
            .select_from(SupplierOfferObservation)
            .where(SupplierOfferObservation.supplier_product_id == supplier_product_id)
        ) or 0

    return {
        "target": {
            "id": target.id,
            "status": target.status,
            "last_checked_at": target.last_checked_at,
            "next_check_at": target.next_check_at,
            "consecutive_failures": target.consecutive_failures,
            "lease_owner": target.lease_owner,
            "lease_until": target.lease_until,
        },
        "last_attempt": None if last_attempt is None else {
            "id": last_attempt.id,
            "outcome": last_attempt.outcome,
            "adapter_code": last_attempt.adapter_code,
            "access_strategy": last_attempt.access_strategy,
            "http_status": last_attempt.http_status,
            "error_code": last_attempt.error_code,
            "error_message": last_attempt.error_message,
            "started_at": last_attempt.started_at,
            "finished_at": last_attempt.finished_at,
            "duration_ms": last_attempt.duration_ms,
        },
        "offer_state": None if state is None else {
            "price": state.price,
            "old_price": state.old_price,
            "available": state.available,
            "stock": state.stock,
            "delivery_days": state.delivery_days,
            "seller": state.seller,
            "version": state.version,
            "observed_at": state.observed_at,
            "last_checked_at": state.last_checked_at,
            "adapter_schema_version": state.adapter_schema_version,
        },
        "observation_count": observation_count,
    }
