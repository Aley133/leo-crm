from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .commerce.repository import SqlAlchemyCommerceRepository
from .commerce.schemas import (
    CommerceOrderLineRead,
    CommerceOrderRead,
    CommerceOrdersResponse,
    CommerceSummaryRead,
)
from .commerce.service import CommerceService
from .db import get_db
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/orders", tags=["workspace-orders"])


@router.get("", response_model=CommerceOrdersResponse)
def list_workspace_orders(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order_status: str | None = Query(default=None, alias="status"),
    query: str | None = Query(default=None, min_length=1, max_length=200),
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> CommerceOrdersResponse:
    service = CommerceService(
        SqlAlchemyCommerceRepository(db, workspace_id=principal.workspace_id)
    )
    total, orders, summary = service.list_orders(
        limit=limit,
        offset=offset,
        status=order_status,
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
