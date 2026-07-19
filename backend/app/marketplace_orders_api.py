from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from .auth import require_service_token
from .db import SessionLocal
from .models import MarketplaceOrder, MarketplaceOrderLine


router = APIRouter(
    prefix="/api/marketplace-orders",
    tags=["marketplace-orders"],
    dependencies=[Depends(require_service_token)],
)


def _money(value: Decimal | float | int) -> str:
    return f"{Decimal(value):.2f}"


def _line_payload(line: MarketplaceOrderLine) -> dict[str, object]:
    return {
        "id": line.id,
        "external_line_id": line.external_line_id,
        "external_product_id": line.external_product_id,
        "merchant_sku": line.merchant_sku,
        "title": line.title,
        "quantity": line.quantity,
        "unit_price": _money(line.unit_price),
        "line_total": _money(line.line_total),
        "product_id": line.product_id,
    }


def _order_payload(order: MarketplaceOrder, *, include_lines: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": order.id,
        "marketplace_account_id": order.marketplace_account_id,
        "external_order_id": order.external_order_id,
        "external_code": order.external_code,
        "status": order.status,
        "original_status": order.original_status,
        "currency": order.currency,
        "total_amount": _money(order.total_amount),
        "ordered_at": order.ordered_at,
        "planned_delivery_at": order.planned_delivery_at,
        "delivered_at": order.delivered_at,
        "source_updated_at": order.source_updated_at,
        "version": order.version,
        "line_count": len(order.lines),
        "total_quantity": sum(line.quantity for line in order.lines),
    }
    if include_lines:
        payload["lines"] = [_line_payload(line) for line in order.lines]
    return payload


@router.get("")
def list_marketplace_orders(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order_status: str | None = Query(default=None, alias="status"),
    query: str | None = Query(default=None, min_length=1, max_length=200),
) -> dict[str, object]:
    filters = []
    if order_status:
        filters.append(MarketplaceOrder.status == order_status)
    if query:
        pattern = f"%{query.strip()}%"
        matching_order_ids = select(MarketplaceOrderLine.marketplace_order_id).where(
            or_(
                MarketplaceOrderLine.merchant_sku.ilike(pattern),
                MarketplaceOrderLine.title.ilike(pattern),
                MarketplaceOrderLine.external_product_id.ilike(pattern),
            )
        )
        filters.append(
            or_(
                MarketplaceOrder.external_code.ilike(pattern),
                MarketplaceOrder.external_order_id.ilike(pattern),
                MarketplaceOrder.id.in_(matching_order_ids),
            )
        )

    with SessionLocal() as session:
        total = session.scalar(select(func.count(MarketplaceOrder.id)).where(*filters)) or 0
        orders = session.scalars(
            select(MarketplaceOrder)
            .where(*filters)
            .options(selectinload(MarketplaceOrder.lines))
            .order_by(MarketplaceOrder.ordered_at.desc().nullslast(), MarketplaceOrder.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_order_payload(order, include_lines=False) for order in orders],
    }


@router.get("/{order_id}")
def get_marketplace_order(order_id: int) -> dict[str, object]:
    with SessionLocal() as session:
        order = session.scalar(
            select(MarketplaceOrder)
            .where(MarketplaceOrder.id == order_id)
            .options(
                selectinload(MarketplaceOrder.lines),
                selectinload(MarketplaceOrder.events),
            )
        )
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Marketplace order not found",
            )
        payload = _order_payload(order, include_lines=True)
        payload["events"] = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "previous_status": event.previous_status,
                "current_status": event.current_status,
                "occurred_at": event.occurred_at,
                "metadata": event.metadata_json,
            }
            for event in sorted(order.events, key=lambda item: item.occurred_at)
        ]
        return payload
