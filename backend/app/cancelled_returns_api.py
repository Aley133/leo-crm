from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .auth import require_service_token
from .cancelled_returns import RECEIVED, TRACKED, WAIT, add_event, event_exists, find_event, utc
from .db import SessionLocal
from .models import MarketplaceOrder, MarketplaceOrderEvent, MarketplaceOrderLine

router = APIRouter(prefix="/api/cancelled-returns", tags=["cancelled-returns"],
                   dependencies=[Depends(require_service_token)])


@router.get("")
def list_returns(view: Literal["pending", "overdue", "received", "all"] = "pending",
                 query: str | None = Query(None, max_length=200),
                 account_id: int | None = None,
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    now = datetime.now(UTC)
    cancelled_at = select(func.min(MarketplaceOrderEvent.occurred_at)).where(
        MarketplaceOrderEvent.marketplace_order_id == MarketplaceOrder.id,
        MarketplaceOrderEvent.event_type == TRACKED,
    ).correlate(MarketplaceOrder).scalar_subquery()
    filters = [event_exists(TRACKED)]
    if view == "received":
        filters.append(event_exists(RECEIVED))
    elif view in {"pending", "overdue"}:
        filters.extend([~event_exists(RECEIVED), MarketplaceOrder.status == "cancelled"])
    if view == "overdue":
        filters.append(cancelled_at <= now - WAIT)
    if account_id is not None:
        filters.append(MarketplaceOrder.marketplace_account_id == account_id)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        filters.append(MarketplaceOrder.external_code.ilike(pattern) | MarketplaceOrder.id.in_(
            select(MarketplaceOrderLine.marketplace_order_id).where(
                MarketplaceOrderLine.title.ilike(pattern) | MarketplaceOrderLine.merchant_sku.ilike(pattern))))
    with SessionLocal() as session:
        total = session.scalar(select(func.count(MarketplaceOrder.id)).where(*filters)) or 0
        amount = session.scalar(select(func.sum(MarketplaceOrder.total_amount)).where(*filters)) or 0
        orders = session.scalars(select(MarketplaceOrder).where(*filters).options(
            selectinload(MarketplaceOrder.events), selectinload(MarketplaceOrder.lines),
            selectinload(MarketplaceOrder.account),
        ).order_by(cancelled_at, MarketplaceOrder.id).offset(offset).limit(limit)).all()
        items = []
        for order in orders:
            tracked = find_event(order, TRACKED)
            received = find_event(order, RECEIVED)
            start = utc(tracked.occurred_at)
            items.append({
                "id": order.id, "code": order.external_code or order.external_order_id,
                "account": order.account.display_name, "amount": str(order.total_amount),
                "cancelled_at": start, "due_at": start + WAIT,
                "days_waiting": max(0, (now - start).days),
                "handoff_at": (tracked.metadata_json or {}).get("handoff_at"),
                "received_at": received.occurred_at if received else None,
                "state": "received" if received else "overdue" if now >= start + WAIT else "waiting",
                "can_receive": order.status == "cancelled" and not received,
                "lines": [{"title": line.title, "quantity": line.quantity, "sku": line.merchant_sku,
                           "product_id": line.product_id} for line in order.lines],
            })
    return {"total": total, "total_amount": str(amount), "items": items, "offset": offset, "limit": limit}


@router.post("/{order_id}/received")
def receive_return(order_id: int):
    with SessionLocal() as session:
        with session.begin():
            order = session.scalar(select(MarketplaceOrder).where(MarketplaceOrder.id == order_id)
                                   .options(selectinload(MarketplaceOrder.events)).with_for_update())
            if order is None:
                raise HTTPException(404, "Заказ не найден")
            received = find_event(order, RECEIVED)
            if received:
                return {"received_at": received.occurred_at, "already_received": True}
            if order.status != "cancelled" or not find_event(order, TRACKED):
                raise HTTPException(409, "Заказ не ожидает возврата после отмены в доставке")
            now = datetime.now(UTC)
            add_event(order, RECEIVED, now, {"source": "operator_confirmation"})
            from .inventory_service import release_cancelled_order_inventory
            release_cancelled_order_inventory(
                session, order=order, released_at=now,
                reason="cancelled_return_physically_received",
            )
    return {"received_at": now, "already_received": False}
