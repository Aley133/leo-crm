from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .commerce.repository import SqlAlchemyCommerceRepository
from .commerce.service import CommerceService
from .db import get_db
from .revenue_models import DailyRevenueSnapshot

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
        account_orders_tuple = tuple(account_orders)
        summary = CommerceService.summarize(account_orders_tuple)
        revenue = summary.revenue.quantize(Decimal("0.01"))
        net_profit = summary.confirmed_net_profit.quantize(Decimal("0.01"))
        margin_pct = (
            (net_profit * Decimal("100") / revenue).quantize(Decimal("0.0001"))
            if revenue > 0
            else Decimal("0")
        )
        snapshot = db.scalar(
            select(DailyRevenueSnapshot).where(
                DailyRevenueSnapshot.marketplace_account_id == account_id,
                DailyRevenueSnapshot.business_date == business_date,
            )
        )
        if snapshot is None:
            snapshot = DailyRevenueSnapshot(
                marketplace_account_id=account_id,
                business_date=business_date,
                timezone=timezone_name,
                source_stage="assembly",
            )
            db.add(snapshot)
        snapshot.orders_count = summary.orders_count
        snapshot.units_count = summary.units_count
        snapshot.revenue = revenue
        snapshot.net_profit = net_profit
        snapshot.margin_pct = margin_pct
        snapshot.order_ids = [order.order_id for order in account_orders]
        snapshot.captured_at = now
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
    items = db.scalars(
        select(DailyRevenueSnapshot)
        .order_by(DailyRevenueSnapshot.business_date.desc(), DailyRevenueSnapshot.id.desc())
        .limit(limit)
    ).all()
    total_revenue = sum((Decimal(item.revenue) for item in items), Decimal("0"))
    total_profit = sum((Decimal(item.net_profit) for item in items), Decimal("0"))
    return {
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
        "items": [_serialize(item) for item in items],
    }
