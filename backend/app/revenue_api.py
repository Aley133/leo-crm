from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .commerce.domain import CommerceOrder
from .commerce.repository import SqlAlchemyCommerceRepository
from .commerce.service import CommerceService
from .db import get_db
from .inventory_models import InventoryBatch
from .models import MarketplaceAccount, Product
from .revenue_models import DailyRevenueSnapshot
from .workspace_context import current_workspace_id

router = APIRouter(
    prefix="/api/revenue",
    tags=["revenue"],
    dependencies=[Depends(require_service_token)],
)


def _serialize(snapshot: DailyRevenueSnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "marketplace_account_id": snapshot.marketplace_account_id,
        "business_date": snapshot.business_date.isoformat(),
        "timezone": snapshot.timezone,
        "source_stage": snapshot.source_stage,
        "orders_count": snapshot.orders_count,
        "units_count": snapshot.units_count,
        "revenue": snapshot.revenue,
        "net_profit": snapshot.net_profit,
        "margin_pct": snapshot.margin_pct,
        "order_ids": snapshot.order_ids,
        "captured_at": snapshot.captured_at,
        "updated_at": snapshot.updated_at,
    }


def _append_new_orders(
    snapshot: DailyRevenueSnapshot,
    orders: list[CommerceOrder],
    *,
    captured_at: datetime,
) -> bool:
    saved_order_ids = list(snapshot.order_ids or [])
    saved_order_id_set = set(saved_order_ids)
    new_orders = tuple(order for order in orders if order.order_id not in saved_order_id_set)
    if not new_orders:
        return False

    added = CommerceService.summarize(new_orders)
    revenue = (Decimal(snapshot.revenue or 0) + added.revenue).quantize(Decimal("0.01"))
    net_profit = (
        Decimal(snapshot.net_profit or 0) + added.confirmed_net_profit
    ).quantize(Decimal("0.01"))

    snapshot.orders_count = int(snapshot.orders_count or 0) + added.orders_count
    snapshot.units_count = int(snapshot.units_count or 0) + added.units_count
    snapshot.revenue = revenue
    snapshot.net_profit = net_profit
    snapshot.margin_pct = (
        (net_profit * Decimal("100") / revenue).quantize(Decimal("0.0001"))
        if revenue > 0
        else Decimal("0")
    )
    snapshot.order_ids = saved_order_ids + [order.order_id for order in new_orders]
    snapshot.captured_at = captured_at
    return True


def _workspace_account_ids(db: Session, workspace_id: int) -> tuple[int, ...]:
    return tuple(
        int(account_id)
        for account_id in db.scalars(
            select(MarketplaceAccount.id).where(
                MarketplaceAccount.workspace_id == workspace_id
            )
        ).all()
    )


def _inventory_analytics(db: Session, workspace_id: int) -> dict[str, object]:
    on_hand_units, on_hand_cost, sku_count = db.execute(
        select(
            func.coalesce(func.sum(InventoryBatch.quantity_remaining), 0),
            func.coalesce(
                func.sum(InventoryBatch.quantity_remaining * InventoryBatch.unit_cost),
                0,
            ),
            func.count(func.distinct(InventoryBatch.product_id)),
        ).where(
            InventoryBatch.workspace_id == workspace_id,
            InventoryBatch.is_received.is_(True),
            InventoryBatch.quantity_remaining > 0,
        )
    ).one()

    incoming_units, incoming_cost = db.execute(
        select(
            func.coalesce(func.sum(InventoryBatch.quantity_remaining), 0),
            func.coalesce(
                func.sum(InventoryBatch.quantity_remaining * InventoryBatch.unit_cost),
                0,
            ),
        ).where(
            InventoryBatch.workspace_id == workspace_id,
            InventoryBatch.is_received.is_(False),
            InventoryBatch.quantity_remaining > 0,
        )
    ).one()

    capital_rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.merchant_sku,
            func.sum(InventoryBatch.quantity_remaining).label("units"),
            func.sum(
                InventoryBatch.quantity_remaining * InventoryBatch.unit_cost
            ).label("inventory_cost"),
            func.min(InventoryBatch.received_at).label("oldest_received_at"),
        )
        .join(Product, Product.id == InventoryBatch.product_id)
        .where(
            InventoryBatch.workspace_id == workspace_id,
            Product.workspace_id == workspace_id,
            InventoryBatch.is_received.is_(True),
            InventoryBatch.quantity_remaining > 0,
        )
        .group_by(Product.id, Product.name, Product.merchant_sku)
        .order_by(
            func.sum(
                InventoryBatch.quantity_remaining * InventoryBatch.unit_cost
            ).desc()
        )
        .limit(8)
    ).all()

    return {
        "on_hand_units": int(on_hand_units or 0),
        "on_hand_cost": Decimal(on_hand_cost or 0).quantize(Decimal("0.01")),
        "sku_count": int(sku_count or 0),
        "incoming_units": int(incoming_units or 0),
        "incoming_cost": Decimal(incoming_cost or 0).quantize(Decimal("0.01")),
        "top_capital": [
            {
                "product_id": int(row.id),
                "name": row.name,
                "merchant_sku": row.merchant_sku,
                "units": int(row.units or 0),
                "inventory_cost": Decimal(row.inventory_cost or 0).quantize(
                    Decimal("0.01")
                ),
                "oldest_received_at": row.oldest_received_at,
            }
            for row in capital_rows
        ],
    }


@router.post("/daily/capture")
def capture_daily_revenue(
    timezone_name: str = Query(default="Asia/Almaty", min_length=1, max_length=64),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    timezone = ZoneInfo(timezone_name)
    business_date = datetime.now(timezone).date()

    service = CommerceService(SqlAlchemyCommerceRepository(db))
    _total, orders, _summary = service.list_orders(
        limit=1000,
        offset=0,
        status="assembly",
        query=None,
    )

    grouped: dict[int, list] = {}
    for order in orders:
        if order.marketplace_account_id is None:
            continue
        grouped.setdefault(order.marketplace_account_id, []).append(order)

    captured: list[DailyRevenueSnapshot] = []
    now = datetime.now(UTC)
    for account_id, account_orders in grouped.items():
        snapshot = db.scalar(
            select(DailyRevenueSnapshot)
            .where(
                DailyRevenueSnapshot.marketplace_account_id == account_id,
                DailyRevenueSnapshot.business_date == business_date,
            )
            .with_for_update()
        )
        if snapshot is None:
            snapshot = DailyRevenueSnapshot(
                marketplace_account_id=account_id,
                business_date=business_date,
                timezone=timezone_name,
                source_stage="assembly",
            )
            db.add(snapshot)
        _append_new_orders(snapshot, account_orders, captured_at=now)
        captured.append(snapshot)

    db.commit()
    for snapshot in captured:
        db.refresh(snapshot)

    return {
        "business_date": business_date.isoformat(),
        "timezone": timezone_name,
        "captured_count": len(captured),
        "items": [_serialize(snapshot) for snapshot in captured],
    }


@router.get("/daily")
def list_daily_revenue(
    limit: int = Query(default=90, ge=1, le=366),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    workspace_id = current_workspace_id()
    account_ids = _workspace_account_ids(db, workspace_id)
    if account_ids:
        items = db.scalars(
            select(DailyRevenueSnapshot)
            .where(DailyRevenueSnapshot.marketplace_account_id.in_(account_ids))
            .order_by(
                DailyRevenueSnapshot.business_date.desc(),
                DailyRevenueSnapshot.id.desc(),
            )
            .limit(limit)
        ).all()
    else:
        items = []

    total_revenue = sum((Decimal(item.revenue) for item in items), Decimal("0"))
    total_profit = sum((Decimal(item.net_profit) for item in items), Decimal("0"))
    return {
        "workspace_id": workspace_id,
        "total": len(items),
        "summary": {
            "revenue": total_revenue,
            "net_profit": total_profit,
            "margin_pct": (
                (total_profit * Decimal("100") / total_revenue).quantize(Decimal("0.0001"))
                if total_revenue > 0
                else Decimal("0")
            ),
        },
        "inventory": _inventory_analytics(db, workspace_id),
        "items": [_serialize(item) for item in items],
    }
