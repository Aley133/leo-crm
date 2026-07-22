from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import require_service_token
from ..browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from ..db import SessionLocal, get_db
from ..kaspi_http_transport import KaspiConfigurationError, KaspiTransportError
from ..kaspi_integration import build_kaspi_order_transport, ensure_kaspi_marketplace_account
from ..marketplace_full_sync import sync_kaspi_orders
from ..models import MarketplaceImportCheckpoint
from .repository import SqlAlchemyCommerceRepository
from .schemas import CommerceOrderLineRead, CommerceOrderRead, CommerceOrdersResponse, CommerceSummaryRead
from .service import CommerceService

router = APIRouter(prefix="/api/commerce", tags=["commerce"], dependencies=[Depends(require_service_token)])


@router.post("/orders/rebuild")
def rebuild_kaspi_orders(days: int = Query(default=7, ge=1, le=31)) -> dict[str, object]:
    """Reload official Kaspi Orders API data. Browser Agent is not involved."""

    try:
        transport = build_kaspi_order_transport()
    except KaspiConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        with SessionLocal() as session:
            with session.begin():
                account = ensure_kaspi_marketplace_account(session)
                marketplace_account_id = account.id
                checkpoint = session.scalar(
                    select(MarketplaceImportCheckpoint).where(
                        MarketplaceImportCheckpoint.marketplace_account_id == marketplace_account_id,
                        MarketplaceImportCheckpoint.stream_name == "orders",
                    )
                )
                if checkpoint is not None:
                    session.delete(checkpoint)

                # Remove only obsolete Kaspi-order jobs from the abandoned attempt.
                session.execute(
                    delete(BrowserAgentJob).where(
                        BrowserAgentJob.status == BrowserAgentJobStatus.QUEUED.value,
                        BrowserAgentJob.url.like("leo-job://kaspi_seller_order_details%"),
                    )
                )

        sync_result = sync_kaspi_orders(
            SessionLocal,
            transport,
            marketplace_account_id=marketplace_account_id,
            page_size=100,
            max_pages=31,
            max_duration_seconds=120,
        )
    except KaspiTransportError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    finally:
        transport.close()

    return {
        "days": days,
        "marketplace_account_id": marketplace_account_id,
        "pages_processed": sync_result.pages_processed,
        "fetched_count": sync_result.fetched_count,
        "imported_count": sync_result.imported_count,
        "updated_count": sync_result.updated_count,
        "api_sync_completed": sync_result.completed,
        "api_sync_stopped_reason": sync_result.stopped_reason,
        "message": "Kaspi Orders API reloaded; stages rebuilt by raw receiver rules",
    }


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
                    )
                    for line in order.lines
                ],
            )
            for order in orders
        ],
    )
