from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..auth import require_service_token
from ..browser_agent_models import BrowserAgentJob
from ..db import SessionLocal, get_db
from ..kaspi_product_enrichment_jobs import (
    create_job as create_product_enrichment_job,
    public_job as public_product_enrichment_job,
    run_job as run_product_enrichment_job,
)
from ..kaspi_raw_receiver_jobs import create_job, public_job, run_job
from .repository import SqlAlchemyCommerceRepository
from .schemas import CommerceOrderLineRead, CommerceOrderRead, CommerceOrdersResponse, CommerceSummaryRead
from .service import CommerceService

router = APIRouter(prefix="/api/commerce", tags=["commerce"], dependencies=[Depends(require_service_token)])


@router.post("/orders/rebuild", status_code=status.HTTP_202_ACCEPTED)
async def rebuild_kaspi_orders(days: int = Query(default=7, ge=1, le=31)) -> dict[str, object]:
    """Start the archive-derived background raw receiver. Browser Agent is not involved."""

    with SessionLocal() as session:
        session.execute(
            delete(BrowserAgentJob).where(
                BrowserAgentJob.url.like("leo-job://kaspi_seller_order_details%")
            )
        )
        session.commit()

    try:
        job_id = create_job(days=days)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    asyncio.create_task(run_job(job_id))
    return {
        "job_id": job_id,
        "status": "queued",
        "days": days,
        "message": "Kaspi raw receiver job queued",
    }


@router.get("/orders/rebuild/{job_id}")
def read_rebuild_job(job_id: str) -> dict[str, object]:
    job = public_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Kaspi raw receiver job not found")
    return job


@router.post("/orders/enrich-products", status_code=status.HTTP_202_ACCEPTED)
async def enrich_kaspi_order_products(
    days: int = Query(default=7, ge=1, le=31),
) -> dict[str, object]:
    """Run archive v1.1.0 product enrichment without Browser Agent."""

    try:
        job_id = create_product_enrichment_job(days=days)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    asyncio.create_task(run_product_enrichment_job(job_id))
    return {
        "job_id": job_id,
        "status": "queued",
        "days": days,
        "message": "Kaspi product enrichment job queued",
    }


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
    query: str | None = Query(default=None, min_length=1, max_length=200),
    db: Session = Depends(get_db),
) -> CommerceOrdersResponse:
    service = CommerceService(SqlAlchemyCommerceRepository(db))
    total, orders, summary = service.list_orders(limit=limit, offset=offset, status=order_status, query=query)
    return CommerceOrdersResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=CommerceSummaryRead(
            orders_count=summary.orders_count,
            units_count=summary.units_count,
            revenue=summary.revenue,
            active_orders=summary.active_orders,
            delivered_orders=summary.delivered_orders,
            cancelled_orders=summary.cancelled_orders,
            unresolved_lines=summary.unresolved_lines,
            procurement_required_lines=summary.procurement_required_lines,
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
                currency=order.currency,
                total_amount=order.total_amount,
                ordered_at=order.ordered_at,
                delivered_at=order.delivered_at,
                units=order.units,
                unresolved_lines=order.unresolved_lines,
                procurement_required_lines=order.procurement_required_lines,
                lines=[
                    CommerceOrderLineRead(
                        line_id=line.line_id,
                        product_id=line.product_id,
                        external_product_id=line.external_product_id,
                        merchant_sku=line.merchant_sku,
                        title=line.title,
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
                        gross_margin=line.gross_margin,
                        gross_margin_pct=line.gross_margin_pct,
                    )
                    for line in order.lines
                ],
            )
            for order in orders
        ],
    )
