from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import require_service_token
from ..browser_agent_models import BrowserAgentJob
from ..db import SessionLocal, get_db
from ..inventory_service import (
    allocated_quantity_for_line,
    rebuild_product_fifo,
    release_cancelled_order_inventory,
)
from ..kaspi_product_enrichment_jobs import (
    create_job as create_product_enrichment_job,
    public_job as public_product_enrichment_job,
    run_job as run_product_enrichment_job,
)
from ..kaspi_order_polling import order_sync_lock
from ..kaspi_raw_receiver_jobs import JOBS as RAW_JOBS
from ..kaspi_raw_receiver_jobs import create_job, public_job, run_job
from ..models import MarketplaceOrder, MarketplaceOrderEvent
from ..product_inventory_group import inventory_owner_product_id
from ..workspace_kaspi import WorkspaceKaspiConnection, load_workspace_kaspi_connection
from .repository import SqlAlchemyCommerceRepository
from .schemas import CommerceOrderLineRead, CommerceOrderRead, CommerceOrdersResponse, CommerceSummaryRead
from .service import CommerceService

router = APIRouter(prefix="/api/commerce", tags=["commerce"], dependencies=[Depends(require_service_token)])


_MANUAL_ORDER_STAGES = {
    "preorder",
    "assembly",
    "handover",
    "shipping",
    "delivered",
    "cancelled",
}
_KASPI_AUTHORITATIVE_STAGES = {
    "handover",
    "shipping",
    "cancelling",
    "delivered",
    "cancelled",
    "returned",
}


class OrderStageOverrideRequest(BaseModel):
    stage: str | None = None
    reason: str | None = Field(default=None, min_length=3, max_length=500)


def _prepare_order_job(*, clear_browser_jobs: bool = False) -> WorkspaceKaspiConnection | None:
    """Load the selected Kaspi account off the event loop."""

    with SessionLocal() as session:
        connection = load_workspace_kaspi_connection(session)
        if connection is not None and clear_browser_jobs:
            session.execute(
                delete(BrowserAgentJob).where(
                    BrowserAgentJob.url.like("leo-job://kaspi_seller_order_details%")
                )
            )
            session.commit()
        return connection


def _rebuild_order_inventory(db: Session, order: MarketplaceOrder) -> int:
    owner_ids = {
        inventory_owner_product_id(db, int(line.product_id))
        for line in order.lines
        if line.product_id is not None
    }
    return sum(
        rebuild_product_fifo(db, product_id=owner_id)
        for owner_id in sorted(owner_ids)
    )


async def _run_full_kaspi_rebuild(
    job_id: str,
    *,
    days: int,
    api_token: str,
    marketplace_account_id: int,
) -> None:
    async with order_sync_lock():
        await run_job(
            job_id,
            api_token=api_token,
            marketplace_account_id=marketplace_account_id,
        )
        raw_job = RAW_JOBS.get(job_id)
        if raw_job is None or raw_job.get("status") == "failed":
            return
        # Orders are already persisted at this point. Product enrichment is useful
        # maintenance, but the Orders screen must not remain locked while it runs.
        raw_job["orders_ready"] = True
        enrichment_job_id = create_product_enrichment_job(
            days=days,
            marketplace_account_id=marketplace_account_id,
        )
        raw_job["status"] = "enriching_products"
        raw_job["enrichment_job_id"] = enrichment_job_id
        raw_job["message"] = "Заказы загружены. Получаем точные названия, артикулы и выполняем складское списание"
        await run_product_enrichment_job(
            enrichment_job_id,
            api_token=api_token,
            marketplace_account_id=marketplace_account_id,
        )
        enrichment = public_product_enrichment_job(enrichment_job_id) or {}
        enrichment_status = str(enrichment.get("status") or "failed")
        enrichment_errors = list(enrichment.get("errors") or [])
        raw_job["product_enrichment"] = {
            "job_id": enrichment_job_id,
            "status": enrichment_status,
            "processed": enrichment.get("processed", 0),
            "total": enrichment.get("total", 0),
            "updated": enrichment.get("updated", 0),
            "linked": enrichment.get("linked", 0),
            "allocated": enrichment.get("allocated", 0),
            "request_count": enrichment.get("request_count", 0),
            "errors": enrichment_errors,
        }
        raw_job["status"] = "completed" if not enrichment_errors else "completed_with_errors"
        raw_job["message"] = (
            f"Готово: заказов {raw_job.get('orders_count', 0)}; "
            f"обновлено товарных строк {enrichment.get('updated', 0)}; "
            f"привязано {enrichment.get('linked', 0)}; "
            f"списано со склада {enrichment.get('allocated', 0)}; "
            f"ошибок enrichment {len(enrichment_errors)}"
        )


@router.post("/orders/rebuild", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_kaspi_orders(days: int = Query(default=7, ge=1, le=31)) -> dict[str, object]:
    connection = await asyncio.to_thread(_prepare_order_job, clear_browser_jobs=True)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kaspi API is not configured for the selected account",
        )
    try:
        job_id = create_job(
            days=days,
            timezone_name=connection.timezone,
            marketplace_account_id=connection.account_id,
            workspace_id=connection.workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    asyncio.create_task(
        _run_full_kaspi_rebuild(
            job_id,
            days=days,
            api_token=connection.api_token,
            marketplace_account_id=connection.account_id,
        )
    )
    return {"job_id": job_id, "status": "queued", "days": days, "message": "Kaspi full order rebuild queued"}


@router.get("/orders/rebuild/{job_id}")
def read_rebuild_job(job_id: str) -> dict[str, object]:
    job = public_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Kaspi raw receiver job not found")
    return job


@router.post("/orders/enrich-products", status_code=status.HTTP_202_ACCEPTED)
async def enrich_kaspi_order_products(days: int = Query(default=7, ge=1, le=31)) -> dict[str, object]:
    connection = await asyncio.to_thread(_prepare_order_job)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kaspi API is not configured for the selected account",
        )
    try:
        job_id = create_product_enrichment_job(
            days=days,
            marketplace_account_id=connection.account_id,
            workspace_id=connection.workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    asyncio.create_task(
        run_product_enrichment_job(
            job_id,
            api_token=connection.api_token,
            marketplace_account_id=connection.account_id,
        )
    )
    return {"job_id": job_id, "status": "queued", "days": days, "message": "Kaspi product enrichment job queued"}


@router.get("/orders/enrich-products/{job_id}")
def read_product_enrichment_job(job_id: str) -> dict[str, object]:
    job = public_product_enrichment_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Kaspi product enrichment job not found")
    return job


@router.get("/orders", response_model=CommerceOrdersResponse)
def list_commerce_orders(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order_status: str | None = Query(default=None, alias="status"),
    kaspi_status: str | None = Query(default=None),
    query: str | None = Query(default=None, min_length=1, max_length=200),
    db: Session = Depends(get_db),
) -> CommerceOrdersResponse:
    service = CommerceService(SqlAlchemyCommerceRepository(db))
    total, orders, summary = service.list_orders(
        limit=limit,
        offset=offset,
        status=order_status,
        source_status=kaspi_status,
        query=query,
    )
    return CommerceOrdersResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=CommerceSummaryRead(
            orders_count=summary.orders_count,
            units_count=summary.units_count,
            revenue=summary.revenue,
            confirmed_net_profit=summary.confirmed_net_profit,
            confirmed_profit_units=summary.confirmed_profit_units,
            active_orders=summary.active_orders,
            delivered_orders=summary.delivered_orders,
            cancelled_orders=summary.cancelled_orders,
            unresolved_lines=summary.unresolved_lines,
            procurement_required_lines=summary.procurement_required_lines,
            procurement_required_units=summary.procurement_required_units,
            incoming_reserved_units=summary.incoming_reserved_units,
        ),
        items=[
            CommerceOrderRead(
                order_id=order.order_id,
                external_code=order.external_code,
                marketplace=order.marketplace,
                marketplace_account_id=order.marketplace_account_id,
                marketplace_external_account_id=order.marketplace_external_account_id,
                status=order.status,
                original_status=order.original_status,
                operational_stage=order.stage.value,
                operational_stage_source=order.stage_source,
                manual_stage=order.manual_stage,
                manual_stage_reason=order.manual_stage_reason,
                manual_stage_updated_at=order.manual_stage_updated_at,
                currency=order.currency,
                total_amount=order.total_amount,
                logistics=order.logistics,
                ordered_at=order.ordered_at,
                delivered_at=order.delivered_at,
                units=order.units,
                unresolved_lines=order.unresolved_lines,
                procurement_required_lines=order.procurement_required_lines,
                procurement_required_units=order.procurement_required_units,
                incoming_reserved_units=order.incoming_reserved_units,
                lines=[
                    CommerceOrderLineRead(
                        line_id=line.line_id,
                        product_id=line.product_id,
                        external_product_id=line.external_product_id,
                        merchant_sku=line.merchant_sku,
                        title=line.title,
                        image_url=line.image_url,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        line_total=line.line_total,
                        is_resolved=line.is_resolved,
                        purchase_request_id=line.purchase_request_id,
                        purchase_status=line.purchase_status,
                        purchase_version=line.purchase_version,
                        procurement_state=order.effective_procurement_state(line).value,
                        procurement_unit_cost=line.procurement_unit_cost,
                        procurement_total_cost=line.procurement_total_cost,
                        procurement_source_name=line.procurement_source_name,
                        inventory_allocated_quantity=line.inventory_allocated_quantity,
                        production_completed_quantity=line.production_completed_quantity,
                        incoming_reserved_quantity=line.incoming_reserved_quantity,
                        uncovered_quantity=line.uncovered_quantity,
                        gross_margin=line.gross_margin,
                        gross_margin_pct=line.gross_margin_pct,
                        kaspi_commission=line.kaspi_commission,
                        tax=line.tax,
                        logistics=line.logistics,
                        net_profit=line.net_profit,
                        net_margin_pct=line.net_margin_pct,
                    )
                    for line in order.lines
                ],
            )
            for order in orders
        ],
    )


@router.post("/orders/{order_id}/stage-override")
def override_order_stage(
    order_id: int,
    payload: OrderStageOverrideRequest,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    order = db.scalar(
        select(MarketplaceOrder)
        .where(MarketplaceOrder.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    target_stage = None if payload.stage is None else payload.stage.strip().lower()
    if target_stage is not None and target_stage not in _MANUAL_ORDER_STAGES:
        raise HTTPException(
            status_code=422,
            detail="Unsupported manual order stage",
        )
    if target_stage is not None and not (payload.reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Укажите причину ручной коррекции",
        )
    if target_stage is not None and order.status in _KASPI_AUTHORITATIVE_STAGES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Этап уже подтверждён Kaspi после передачи со склада; "
                "ручная коррекция назад запрещена"
            ),
        )

    previous_stage = order.manual_stage
    now = datetime.now(UTC)
    order.manual_stage = target_stage
    order.manual_stage_reason = (
        (payload.reason or "").strip() or None
        if target_stage is not None
        else None
    )
    order.manual_stage_updated_at = now if target_stage is not None else None
    order.version += 1

    if target_stage == "cancelled":
        release_cancelled_order_inventory(
            db,
            order=order,
            released_at=now,
            force=True,
            reason="manual_owner_cancellation",
        )
    else:
        _rebuild_order_inventory(db, order)

    if target_stage == "assembly":
        shortages = [
            line
            for line in order.lines
            if line.product_id is None
            or allocated_quantity_for_line(db, int(line.id)) < int(line.quantity or 0)
        ]
        if shortages:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Нельзя перевести заказ в упаковку: FIFO не покрывает "
                    f"{len(shortages)} товарных позиций"
                ),
            )

    order.events.append(
        MarketplaceOrderEvent(
            source_event_key=(
                f"manual_stage:{order.version}:{target_stage or 'kaspi_auto'}"
            ),
            event_type=(
                "manual_stage_cleared"
                if target_stage is None
                else "manual_stage_changed"
            ),
            previous_status=previous_stage or order.status,
            current_status=target_stage or order.status,
            occurred_at=now,
            metadata_json={
                "reason": (payload.reason or "").strip() or "return_to_kaspi_truth",
                "source_status": order.status,
                "source_original_status": order.original_status,
            },
        )
    )
    db.commit()
    return {
        "order_id": order.id,
        "manual_stage": order.manual_stage,
        "manual_stage_reason": order.manual_stage_reason,
        "manual_stage_updated_at": order.manual_stage_updated_at,
        "source_status": order.status,
        "reconciled": True,
    }
