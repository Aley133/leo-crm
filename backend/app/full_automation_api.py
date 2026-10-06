from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .auth import require_service_token
from .db import get_db
from .models import Product
from .dumping_models import DumpingPolicy
from .fast_dumping_models import FastDumpingPolicy, FastDumpingJob, FastDumpingState
from .fast_dumping_service import ensure_state, cancel_active_job, queue_scan, utcnow
from .fast_dumping_api import list_fast_dumping_products
from .workspace_context import current_workspace_id

router = APIRouter(
    prefix="/api/full-automation",
    tags=["full-automation"],
    dependencies=[Depends(require_service_token)],
)
CAPACITY = 8
SNAPSHOT_FIELDS = (
    "enabled",
    "minimum_profit_kzt",
    "scan_interval_seconds",
    "undercut_step_kzt",
    "allow_price_raise",
    "max_undercut_gap_percent",
    "delivery_price_premium_kzt",
    "delivery_advantage_days",
    "owned_price_band_kzt",
    "preorder_target_position",
)


class AutomationSettings(BaseModel):
    enabled: bool = True
    minimum_profit_kzt: Decimal = Field(default=1000, ge=0, le=100000000)
    monitor_seconds: Literal[60, 120, 180, 300] = 120
    premium_per_day_kzt: int = Field(default=500, ge=0, le=10000)
    premium_cap_kzt: int = Field(default=2000, ge=0, le=100000)
    delivery_advantage_days: int = Field(default=4, ge=1, le=30)
    maximum_price_kzt: int | None = Field(default=None, gt=0, le=100000000)
    observation_hours: int = Field(default=12, ge=1, le=72)


@router.get("")
def read_automation(db: Session = Depends(get_db)):
    result = list_fast_dumping_products(db=db)
    policies = db.scalars(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.workspace_id == current_workspace_id()
        )
    ).all()
    by_id = {p.product_id: p for p in policies}
    states = {
        s.product_id: s
        for s in db.scalars(
            select(FastDumpingState).where(
                FastDumpingState.workspace_id == current_workspace_id()
            )
        ).all()
    }
    for item in result["items"]:
        p = by_id[item["product_id"]]
        item["automation"] = {
            "enabled": p.pricing_mode == "automation",
            "settings": p.automation_config or {},
        }
        state = states.get(p.product_id)
        item["automation"]["observation"] = (
            (state.automation_json or {}) if state is not None else {}
        )
    result["capacity"] = CAPACITY
    return result


@router.put("/products/{product_id}")
def save_automation(
    product_id: int, payload: AutomationSettings, db: Session = Depends(get_db)
):
    workspace = current_workspace_id()
    # Lock the tenant root to serialize capacity checks and mode transitions.
    from .workspace_models import Workspace

    db.scalar(select(Workspace).where(Workspace.id == workspace).with_for_update())
    product = db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.workspace_id == workspace)
        .with_for_update()
    )
    if product is None:
        raise HTTPException(404, "Товар не найден")
    if not product.merchant_sku:
        raise HTTPException(409, "Для автоматизации нужен Merchant SKU")
    policy = db.scalar(
        select(FastDumpingPolicy)
        .where(
            FastDumpingPolicy.product_id == product_id,
            FastDumpingPolicy.workspace_id == workspace,
        )
        .with_for_update()
    )
    new_policy = policy is None
    if policy is None:
        if not payload.enabled:
            return {"enabled": False}
        policy = FastDumpingPolicy(product_id=product_id, workspace_id=workspace)
        db.add(policy)
        db.flush()
    state = ensure_state(db, policy=policy, workspace_id=workspace)
    active = (
        db.get(FastDumpingJob, state.active_job_id) if state.active_job_id else None
    )
    if active is not None and active.status in {
        "leased_apply",
        "leased_verify",
        "queued_verify",
    }:
        raise HTTPException(
            409, "Дождитесь подтверждения текущей операции Kaspi перед сменой режима"
        )
    if payload.enabled and policy.pricing_mode != "automation":
        count = db.scalar(
            select(func.count())
            .select_from(FastDumpingPolicy)
            .where(
                FastDumpingPolicy.workspace_id == workspace,
                FastDumpingPolicy.pricing_mode == "automation",
                FastDumpingPolicy.enabled.is_(True),
            )
        )
        if count >= CAPACITY:
            raise HTTPException(
                409,
                f"Быстрый канал рассчитан на {CAPACITY} активных товаров. Отключите другой товар перед подключением.",
            )
    classic = db.scalar(
        select(DumpingPolicy)
        .where(DumpingPolicy.product_id == product_id)
        .with_for_update()
    )
    cancel_active_job(db, state=state, reason="Изменён режим полной автоматизации")
    config = dict(policy.automation_config or {})
    if payload.enabled:
        if policy.pricing_mode != "automation":
            config["previous"] = {
                k: (
                    str(getattr(policy, k))
                    if isinstance(getattr(policy, k), Decimal)
                    else getattr(policy, k)
                )
                for k in SNAPSHOT_FIELDS
            }
            if new_policy:
                config["previous"]["enabled"] = False
            config["classic_enabled"] = bool(classic and classic.enabled)
        if classic:
            classic.enabled = False
        config.update(payload.model_dump(mode="json", exclude={"enabled"}))
        policy.pricing_mode = "automation"
        policy.enabled = True
        policy.minimum_profit_kzt = payload.minimum_profit_kzt
        policy.automation_config = config
        state.automation_json = None
    elif policy.pricing_mode == "automation":
        for k, v in config.get("previous", {}).items():
            setattr(
                policy,
                k,
                (
                    Decimal(str(v))
                    if k in {"minimum_profit_kzt", "max_undercut_gap_percent"}
                    else v
                ),
            )
        policy.pricing_mode = "manual"
        policy.automation_config = None
        if classic:
            classic.enabled = bool(config.get("classic_enabled", False))
    state.state_version += 1
    state.next_scan_at = utcnow() if policy.enabled else None
    if policy.enabled and not state.automatic_writes_paused:
        queue_scan(
            db, policy=policy, workspace_id=workspace, reason="automation_mode_changed"
        )
    db.commit()
    return {"product_id": product_id, "enabled": policy.pricing_mode == "automation"}
