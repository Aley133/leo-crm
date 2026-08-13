from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_service import (
    calculate_safe_floor,
    physical_stock_counts,
    resolve_cost_sources,
)
from .fast_dumping_models import (
    FastDumpingJob,
    FastDumpingPolicy,
    FastDumpingState,
)
from .fast_dumping_service import (
    cancel_active_job,
    ensure_state,
    queue_scan,
    resume_automatic_writes,
    utcnow,
)
from .models import Product
from .product_inventory_group import inventory_owner_ids_for_products
from .workspace_context import current_workspace_id


class FastDumpingPolicyUpsert(BaseModel):
    enabled: bool = True
    minimum_profit_kzt: Decimal = Field(default=1000, ge=0, le=100000000)
    undercut_step_kzt: int = Field(default=1, ge=1, le=10000)
    allow_price_raise: bool = True
    max_undercut_gap_percent: Decimal = Field(default=35, gt=0, le=100)
    scan_interval_seconds: Literal[300, 600] = 600
    delivery_price_premium_kzt: int = Field(default=500, ge=0, le=100000)
    delivery_advantage_days: int = Field(default=5, ge=1, le=30)
    city_id: str = Field(default="750000000", min_length=1, max_length=32)
    zone_id: str = Field(default="Magnum_ZONE1", min_length=1, max_length=64)


router = APIRouter(
    prefix="/api/fast-dumping",
    tags=["fast-dumping"],
    dependencies=[Depends(require_service_token)],
)

ATTENTION_STATUSES = {
    "floor_limited",
    "price_anomaly",
    "market_context_mismatch",
    "own_offer_missing",
    "out_of_stock",
    "apply_timeout",
    "apply_unconfirmed",
    "error",
}
WORKING_STATUSES = {
    "queued",
    "scanning",
    "queued_apply",
    "preparing_apply",
    "applying",
    "verifying",
}


def _policy_payload(policy: FastDumpingPolicy) -> dict:
    return {
        "id": policy.id,
        "product_id": policy.product_id,
        "enabled": policy.enabled,
        "minimum_profit_kzt": policy.minimum_profit_kzt,
        "undercut_step_kzt": policy.undercut_step_kzt,
        "allow_price_raise": policy.allow_price_raise,
        "max_undercut_gap_percent": policy.max_undercut_gap_percent,
        "scan_interval_seconds": policy.scan_interval_seconds,
        "delivery_price_premium_kzt": policy.delivery_price_premium_kzt,
        "delivery_advantage_days": policy.delivery_advantage_days,
        "city_id": policy.city_id,
        "zone_id": policy.zone_id,
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
    }


def _state_payload(
    state: FastDumpingState | None,
    *,
    include_offers: bool = False,
) -> dict | None:
    if state is None:
        return None
    payload = {
        "id": state.id,
        "status": state.status,
        "decision_status": state.decision_status,
        "status_reason": state.status_reason,
        "source_kind": state.source_kind,
        "source_name": state.source_name,
        "source_cost_kzt": state.source_cost_kzt,
        "inventory_on_hand": state.inventory_on_hand,
        "safe_floor_kzt": state.safe_floor_kzt,
        "own_price_kzt": state.own_price_kzt,
        "competitor_price_kzt": state.competitor_price_kzt,
        "competitor_name": state.competitor_name,
        "target_price_kzt": state.target_price_kzt,
        "desired_stock_count": state.desired_stock_count,
        "own_position": state.own_position,
        "seller_count": state.seller_count,
        "product_url": state.product_url,
        "product_model": state.product_model,
        "page_visible_price_kzt": state.page_visible_price_kzt,
        "market_context_ok": state.market_context_ok,
        "market_context_reason": state.market_context_reason,
        "offers_count": state.offers_count,
        "state_version": state.state_version,
        "active_job_id": state.active_job_id,
        "automatic_writes_paused": state.automatic_writes_paused,
        "pause_reason": state.pause_reason,
        "last_operation_id": state.last_operation_id,
        "last_agent_id": state.last_agent_id,
        "last_error_code": state.last_error_code,
        "last_error_message": state.last_error_message,
        "last_scanned_at": state.last_scanned_at,
        "last_applied_at": state.last_applied_at,
        "next_scan_at": state.next_scan_at,
        "updated_at": state.updated_at,
    }
    if include_offers:
        payload["offers"] = state.offers_json or []
    return payload


@router.get("")
def list_fast_dumping_products(db: Session = Depends(get_db)) -> dict:
    workspace_id = current_workspace_id()
    rows = db.execute(
        select(FastDumpingPolicy, Product, FastDumpingState)
        .join(Product, Product.id == FastDumpingPolicy.product_id)
        .outerjoin(
            FastDumpingState,
            FastDumpingState.policy_id == FastDumpingPolicy.id,
        )
        .where(
            FastDumpingPolicy.workspace_id == workspace_id,
            Product.workspace_id == workspace_id,
        )
        .order_by(FastDumpingPolicy.updated_at.desc(), FastDumpingPolicy.id.desc())
    ).all()
    product_ids = {int(product.id) for _policy, product, _state in rows}
    owner_by_product = inventory_owner_ids_for_products(db, product_ids)
    stock_counts = physical_stock_counts(
        db,
        product_ids=product_ids,
        owner_by_product=owner_by_product,
    )
    try:
        sources = resolve_cost_sources(
            db,
            product_ids=product_ids,
            owner_by_product=owner_by_product,
        )
    except Exception:
        sources = {}

    items: list[dict] = []
    for policy, product, state in rows:
        source = sources.get(int(product.id))
        stock = int(stock_counts.get(int(product.id), 0))
        current_floor = None
        if source is not None and source.kind == "inventory" and stock > 0:
            current_floor = calculate_safe_floor(
                unit_cost_kzt=source.unit_cost_kzt,
                minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
            )
        items.append(
            {
                "product_id": product.id,
                "name": product.name,
                "brand": product.brand,
                "kaspi_product_id": product.kaspi_product_id,
                "merchant_sku": product.merchant_sku,
                "sale_enabled": bool(product.sale_enabled),
                "policy": _policy_payload(policy),
                "state": _state_payload(state),
                "current_inventory_on_hand": stock,
                "current_source": (
                    None
                    if source is None
                    else {
                        "kind": source.kind,
                        "name": source.name,
                        "unit_cost_kzt": source.unit_cost_kzt,
                        "delivery_days": source.delivery_days,
                    }
                ),
                "current_safe_floor_kzt": current_floor,
            }
        )

    def row_status(row: dict) -> str:
        state = row.get("state") or {}
        return str(state.get("status") or "idle")

    summary = {
        "total": len(items),
        "enabled": sum(bool(row["policy"]["enabled"]) for row in items),
        "floor_limited": sum(
            row_status(row) == "floor_limited"
            or (row.get("state") or {}).get("decision_status") == "floor_limited"
            for row in items
        ),
        "attention": sum(row_status(row) in ATTENTION_STATUSES for row in items),
        "working": sum(row_status(row) in WORKING_STATUSES for row in items),
        "paused_writes": sum(
            bool((row.get("state") or {}).get("automatic_writes_paused"))
            for row in items
        ),
    }
    return {"items": items, "summary": summary, "checked_at": datetime.now(UTC)}


@router.put("/products/{product_id}")
def upsert_fast_dumping_policy(
    product_id: int,
    payload: FastDumpingPolicyUpsert,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.workspace_id == workspace_id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if not product.merchant_sku:
        raise HTTPException(
            status_code=409,
            detail="Для realtime-записи у товара должен быть merchant SKU.",
        )
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.workspace_id == workspace_id,
            FastDumpingPolicy.product_id == product_id,
        )
    )
    if policy is None:
        policy = FastDumpingPolicy(
            workspace_id=workspace_id,
            product_id=product_id,
        )
        db.add(policy)
        db.flush()
    state = ensure_state(db, policy=policy, workspace_id=workspace_id)
    active = db.get(FastDumpingJob, state.active_job_id) if state.active_job_id else None
    if active is not None and active.status in {"leased_apply", "leased_verify"}:
        raise HTTPException(
            status_code=409,
            detail="Сейчас подтверждается realtime-операция. Дождитесь результата перед изменением порога.",
        )
    cancel_active_job(
        db,
        state=state,
        reason="Настройки быстрого демпинга изменены пользователем.",
    )
    for field, value in payload.model_dump().items():
        setattr(policy, field, value)
    if policy.enabled and state.automatic_writes_paused:
        state.status = "apply_unconfirmed"
        state.status_reason = state.pause_reason or "Автозапись приостановлена."
        state.next_scan_at = None
    else:
        state.status = "idle" if policy.enabled else "paused"
        state.status_reason = (
            "Настройки сохранены; ожидается новая проверка."
            if policy.enabled
            else "Быстрый демпинг выключен пользователем."
        )
        state.next_scan_at = utcnow() if policy.enabled else None
    db.flush()
    queued = False
    if policy.enabled and not state.automatic_writes_paused:
        _job, queued = queue_scan(
            db,
            policy=policy,
            workspace_id=workspace_id,
            reason="policy_saved",
        )
    db.commit()
    db.refresh(policy)
    db.refresh(state)
    return {
        "product_id": product_id,
        "policy": _policy_payload(policy),
        "state": _state_payload(state),
        "queued": queued,
    }


@router.get("/products/{product_id}/offers")
def read_fast_dumping_offers(
    product_id: int,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    state = db.scalar(
        select(FastDumpingState)
        .join(Product, Product.id == FastDumpingState.product_id)
        .where(
            FastDumpingState.workspace_id == workspace_id,
            FastDumpingState.product_id == product_id,
            Product.workspace_id == workspace_id,
        )
    )
    if state is None:
        raise HTTPException(status_code=404, detail="Fast dumping state not found")
    return {
        "product_id": product_id,
        "state_version": state.state_version,
        "market_context_ok": state.market_context_ok,
        "market_context_reason": state.market_context_reason,
        "offers": state.offers_json or [],
    }


@router.post(
    "/products/{product_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
)
def run_fast_dumping_now(
    product_id: int,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.workspace_id == workspace_id,
            FastDumpingPolicy.product_id == product_id,
        )
    )
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=409, detail="Быстрый демпинг для товара выключен")
    state = ensure_state(db, policy=policy, workspace_id=workspace_id)
    if state.automatic_writes_paused:
        raise HTTPException(
            status_code=409,
            detail=state.pause_reason or "Сначала снимите защитную паузу.",
        )
    next_scan_at = state.next_scan_at
    if next_scan_at is not None:
        if next_scan_at.tzinfo is None:
            next_scan_at = next_scan_at.replace(tzinfo=UTC)
        if next_scan_at > utcnow():
            return {
                "status": "cooldown",
                "queued": False,
                "product_id": product_id,
                "next_scan_at": next_scan_at,
            }
    _job, queued = queue_scan(
        db,
        policy=policy,
        workspace_id=workspace_id,
        reason="manual",
    )
    db.commit()
    return {
        "status": "queued" if queued else "already_active",
        "queued": queued,
        "product_id": product_id,
    }


@router.post("/products/{product_id}/resume")
def resume_fast_dumping_product(
    product_id: int,
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = current_workspace_id()
    policy = db.scalar(
        select(FastDumpingPolicy).where(
            FastDumpingPolicy.workspace_id == workspace_id,
            FastDumpingPolicy.product_id == product_id,
        )
    )
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=409, detail="Быстрый демпинг для товара выключен")
    try:
        state = resume_automatic_writes(
            db,
            workspace_id=workspace_id,
            product_id=product_id,
        )
        _job, queued = queue_scan(
            db,
            policy=policy,
            workspace_id=workspace_id,
            reason="manual_resume",
        )
        db.commit()
        return {"status": state.status, "queued": queued}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
