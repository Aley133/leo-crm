from datetime import UTC, datetime, timedelta
from sqlalchemy import select, func, or_, and_
from .models import MarketplaceOrder, MarketplaceOrderLine, MarketplaceAccount, Product


def observe_sales(db, *, policy, state):
    """Observe sales without sacrificing the profit-first TOP-3 objective."""
    now = datetime.now(UTC)
    data = dict(state.automation_json or {})
    price = str(state.own_price_kzt)
    if data.get("observed_price") != price:
        data.update(observed_price=price, exposure_started_at=now.isoformat())
    try:
        started = datetime.fromisoformat(data["exposure_started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
    except (KeyError, ValueError):
        started = now
        data["exposure_started_at"] = now.isoformat()
    window = timedelta(
        hours=int((policy.automation_config or {}).get("observation_hours", 12))
    )
    since = max(started, now - timedelta(hours=72))
    product = db.get(Product, policy.product_id)
    quantity = (
        db.scalar(
            select(func.coalesce(func.sum(MarketplaceOrderLine.quantity), 0))
            .join(
                MarketplaceOrder,
                MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id,
            )
            .join(
                MarketplaceAccount,
                MarketplaceAccount.id == MarketplaceOrder.marketplace_account_id,
            )
            .where(
                MarketplaceOrderLine.workspace_id == policy.workspace_id,
                MarketplaceOrder.workspace_id == policy.workspace_id,
                MarketplaceAccount.workspace_id == policy.workspace_id,
                MarketplaceAccount.provider == "kaspi",
                or_(
                    MarketplaceOrderLine.product_id == policy.product_id,
                    and_(
                        bool(product and product.merchant_sku),
                        MarketplaceOrderLine.product_id.is_(None),
                        MarketplaceOrderLine.merchant_sku == product.merchant_sku,
                    ),
                ),
                MarketplaceOrder.ordered_at >= since,
                MarketplaceOrder.ordered_at <= now,
                MarketplaceOrder.status.in_(
                    ["new", "accepted", "assembly", "handover", "shipping", "delivered"]
                ),
            )
        )
        or 0
    )
    tier = 3
    if now - started >= window:
        data["exposure_started_at"] = now.isoformat()
    data.update(
        target_position=tier,
        orders_units=int(quantity),
        sales_checked_at=now.isoformat(),
        sales_note=(
            "Есть заказы: ищем более прибыльную цену."
            if quantity
            else "Заказов за период наблюдения нет. Причина неизвестна без данных о просмотрах и спросе."
        ),
    )
    state.automation_json = data
    return data
