from decimal import Decimal
from typing import Any


def build_purchase_plan(*, current: dict, previous: dict, inventory: dict,
                        delivered_orders: int) -> dict[str, Any]:
    """Conservative 30-day replenishment; never scale demand to a sales target."""
    buy, recover, hold, leaders = [], [], [], []
    items = {row["product_id"]: row for row in inventory["items"]}
    for key in set(current) | set(previous) | set(items):
        stat, old, inv = current.get(key, {}), previous.get(key, {}), items.get(key, {})
        units = int(stat.get("delivered_units", 0))
        old_units = int(old.get("delivered_units", 0))
        stock, incoming = int(inv.get("on_hand_units", 0)), int(inv.get("incoming_units", 0))
        lost = units == 0 and old_units > 0
        basis = old if lost else stat
        demand_units = old_units if lost else units
        price = inv.get("last_purchase_price") or inv.get("weighted_purchase_price")
        price = Decimal(price) if price is not None else None
        profit = Decimal(basis.get("known_net_profit", 0))
        cost_known = demand_units > 0 and int(basis.get("unpriced_units", 0)) == 0
        row = {
            "product_id": inv.get("product_id") or stat.get("product_id") or old.get("product_id"),
            "name": inv.get("name") or stat.get("name") or old.get("name") or "Неизвестный товар",
            "on_hand_units": stock, "incoming_units": incoming,
            "sold_units": units, "previous_units": old_units,
            "orders_per_day": round(Decimal(len(basis.get("orders", set()))) / 30, 2),
            "purchase_units": max(demand_units - stock - incoming, 0),
            "purchase_price": price,
            "inventory_value": inv.get("inventory_value", 0),
            "profit_per_unit": round(profit / demand_units, 2) if cost_known else None,
            "days_of_stock": round(Decimal(stock) * 30 / units, 1) if units else None,
        }
        row["purchase_amount"] = round(price * row["purchase_units"], 2) if price is not None else None
        if units and cost_known and profit > 0:
            leaders.append(dict(row))
        if lost:
            row["action"] = (
                "Есть остаток: проверить цену, доставку и активность карточки; закупка не нужна."
                if stock else "Товар уже в пути: проверить срок поставки и карточку."
                if incoming else "Проверить спрос и карточку перед закупкой; количество по прежним продажам."
            )
            if key not in items or stock or incoming or not cost_known or profit <= 0 or price is None or price <= 0:
                row["purchase_units"] = 0
                row["purchase_amount"] = None
            if not cost_known or price is None or price <= 0:
                row["action"] += " Для расчёта закупки заполнить себестоимость и цену."
            elif profit <= 0:
                row["action"] += " Закупку остановить: прежние продажи не давали положительной прибыли."
            recover.append(row)
        elif demand_units and (row["purchase_units"] > 0 or not cost_known or profit <= 0):
            if key not in items or not cost_known or profit <= 0 or price is None or price <= 0:
                row["action"] = "Заполнить закупочную цену и себестоимость." if not cost_known or price is None or price <= 0 else "Закупку остановить: продажи не дают положительной прибыли."
                row["purchase_units"] = 0
                row["purchase_amount"] = None
                hold.append(row)
            else:
                row["action"] = "Докупить запас на 30 дней по текущим продажам."
                buy.append(row)
        elif stock and units == 0:
            row["action"] = "Не докупать: нет выданных продаж за 30 дней. Проверить цену и карточку."
            hold.append(row)
    buy.sort(key=lambda r: (r["days_of_stock"] or 0, -r["orders_per_day"]))
    recover.sort(key=lambda r: -r["orders_per_day"])
    hold.sort(key=lambda r: -Decimal(r["inventory_value"]))
    leaders.sort(key=lambda r: (-r["orders_per_day"], -r["profit_per_unit"]))
    daily = Decimal(delivered_orders) / 30
    return {
        "history_days": 30, "coverage_days": 30,
        "orders_per_day": round(daily, 2),
        "goals": [{"target": target, "gap": round(max(Decimal(target) - daily, 0), 2)}
                  for target in (100, 200)],
        "purchase_amount": sum((r["purchase_amount"] for r in buy), Decimal("0")),
        "recovery_amount": sum((r["purchase_amount"] or Decimal("0") for r in recover), Decimal("0")),
        "buy": buy, "recover": recover, "hold": hold, "leaders": leaders[:5],
    }
