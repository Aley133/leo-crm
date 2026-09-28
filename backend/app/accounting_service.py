from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .commerce.profit_calculator import (
    KASPI_COMMISSION_RATE,
    TAX_RATE,
    allocate_order_logistics,
    kaspi_logistics_per_unit,
)
from .inventory_models import InventoryAllocation, InventoryBatch, InventoryBatchType
from .models import MarketplaceOrder, MarketplaceOrderLine, Product


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
_DELIVERED = "delivered"
_CANCELLED = {"cancelling", "cancelled"}
_RETURNED = "returned"


def _money(value: Decimal | int | float) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _percent(value: Decimal | int | float) -> Decimal:
    return Decimal(value).quantize(PERCENT, rounding=ROUND_HALF_UP)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _change_pct(current: Decimal | int, previous: Decimal | int) -> Decimal | None:
    previous_value = Decimal(previous)
    if previous_value == 0:
        return None
    return _percent((Decimal(current) - previous_value) * Decimal("100") / abs(previous_value))


def _owner_id(product: Product, products_by_id: dict[int, Product]) -> int:
    candidate = int(product.inventory_owner_product_id or product.id)
    return candidate if candidate in products_by_id else int(product.id)


def build_inventory_snapshot(
    db: Session,
    *,
    workspace_id: int,
    include_zero: bool = True,
) -> dict[str, Any]:
    """Build a read-only physical inventory valuation without duplicating shared stock."""

    products = list(
        db.scalars(
            select(Product)
            .where(Product.workspace_id == workspace_id)
            .order_by(Product.id)
        ).all()
    )
    products_by_id = {int(product.id): product for product in products}
    owner_by_product = {
        int(product.id): _owner_id(product, products_by_id) for product in products
    }
    members_by_owner: dict[int, list[Product]] = defaultdict(list)
    for product in products:
        members_by_owner[owner_by_product[int(product.id)]].append(product)

    current_batches = list(
        db.scalars(
            select(InventoryBatch).where(
                InventoryBatch.workspace_id == workspace_id,
                InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
                InventoryBatch.is_received.is_(True),
                InventoryBatch.quantity_remaining > 0,
            )
        ).all()
    )
    incoming_batches = list(
        db.scalars(
            select(InventoryBatch).where(
                InventoryBatch.workspace_id == workspace_id,
                InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
                InventoryBatch.is_received.is_(False),
                InventoryBatch.quantity_received > 0,
            )
        ).all()
    )
    ranked_priced_batches = (
        select(
            InventoryBatch.id.label("batch_id"),
            func.row_number()
            .over(
                partition_by=InventoryBatch.product_id,
                order_by=(
                    InventoryBatch.received_at.desc(),
                    InventoryBatch.id.desc(),
                ),
            )
            .label("batch_rank"),
        )
        .where(
            InventoryBatch.workspace_id == workspace_id,
            InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
            InventoryBatch.is_received.is_(True),
            InventoryBatch.unit_cost > 0,
        )
        .subquery("ranked_accounting_purchase_batches")
    )
    priced_history = list(
        db.scalars(
            select(InventoryBatch)
            .join(
                ranked_priced_batches,
                ranked_priced_batches.c.batch_id == InventoryBatch.id,
            )
            .where(ranked_priced_batches.c.batch_rank == 1)
        ).all()
    )

    current_by_owner: dict[int, list[InventoryBatch]] = defaultdict(list)
    incoming_by_owner: dict[int, list[InventoryBatch]] = defaultdict(list)
    latest_priced_by_owner: dict[int, InventoryBatch] = {}

    def normalized_batch_owner(batch: InventoryBatch) -> int:
        return owner_by_product.get(int(batch.product_id), int(batch.product_id))

    for batch in current_batches:
        current_by_owner[normalized_batch_owner(batch)].append(batch)
    for batch in incoming_batches:
        incoming_by_owner[normalized_batch_owner(batch)].append(batch)
    for batch in priced_history:
        owner_id = normalized_batch_owner(batch)
        previous = latest_priced_by_owner.get(owner_id)
        marker = (_aware(batch.received_at) or datetime.min.replace(tzinfo=UTC), int(batch.id))
        previous_marker = (
            (_aware(previous.received_at) or datetime.min.replace(tzinfo=UTC)),
            int(previous.id),
        ) if previous is not None else None
        if previous_marker is None or marker > previous_marker:
            latest_priced_by_owner[owner_id] = batch

    owner_ids = set(members_by_owner) | set(current_by_owner) | set(incoming_by_owner)
    rows: list[dict[str, Any]] = []
    for owner_id in sorted(owner_ids):
        members = members_by_owner.get(owner_id, [])
        owner = products_by_id.get(owner_id) or (members[0] if members else None)
        if owner is None:
            continue

        batches = current_by_owner.get(owner_id, [])
        incoming = incoming_by_owner.get(owner_id, [])
        on_hand_units = sum(max(int(batch.quantity_remaining or 0), 0) for batch in batches)
        priced_on_hand_units = sum(
            max(int(batch.quantity_remaining or 0), 0)
            for batch in batches
            if Decimal(batch.unit_cost or 0) > 0
        )
        unpriced_units = on_hand_units - priced_on_hand_units
        inventory_value = sum(
            Decimal(batch.unit_cost or 0) * max(int(batch.quantity_remaining or 0), 0)
            for batch in batches
            if Decimal(batch.unit_cost or 0) > 0
        )
        weighted_purchase_price = (
            _money(inventory_value / Decimal(priced_on_hand_units))
            if priced_on_hand_units > 0
            else None
        )

        incoming_units = sum(max(int(batch.quantity_received or 0), 0) for batch in incoming)
        incoming_priced_units = sum(
            max(int(batch.quantity_received or 0), 0)
            for batch in incoming
            if Decimal(batch.unit_cost or 0) > 0
        )
        incoming_value = sum(
            Decimal(batch.unit_cost or 0) * max(int(batch.quantity_received or 0), 0)
            for batch in incoming
            if Decimal(batch.unit_cost or 0) > 0
        )
        latest = latest_priced_by_owner.get(owner_id)
        received_dates = [_aware(batch.received_at) for batch in batches if batch.received_at]
        member_skus = sorted(
            {str(item.merchant_sku).strip() for item in members if item.merchant_sku}
        )
        member_kaspi_ids = sorted(
            {str(item.kaspi_product_id).strip() for item in members if item.kaspi_product_id}
        )
        primary_sku = (owner.merchant_sku or "").strip() or (member_skus[0] if member_skus else None)
        batch_rows = [
            {
                "batch_id": int(batch.id),
                "received_at": _aware(batch.received_at),
                "quantity_received": int(batch.quantity_received or 0),
                "quantity_remaining": int(batch.quantity_remaining or 0),
                "unit_cost": _money(Decimal(batch.unit_cost or 0)),
                "remaining_value": _money(
                    Decimal(batch.unit_cost or 0)
                    * max(int(batch.quantity_remaining or 0), 0)
                ),
                "source_name": batch.source_name,
                "reference": batch.reference,
                "note": batch.note,
                "state": "on_hand",
            }
            for batch in sorted(
                batches,
                key=lambda item: (
                    _aware(item.received_at) or datetime.min.replace(tzinfo=UTC),
                    int(item.id),
                ),
            )
        ]
        incoming_batch_rows = [
            {
                "batch_id": int(batch.id),
                "received_at": _aware(batch.received_at),
                "quantity_received": int(batch.quantity_received or 0),
                "quantity_remaining": 0,
                "unit_cost": _money(Decimal(batch.unit_cost or 0)),
                "remaining_value": _money(
                    Decimal(batch.unit_cost or 0)
                    * max(int(batch.quantity_received or 0), 0)
                ),
                "source_name": batch.source_name,
                "reference": batch.reference,
                "note": batch.note,
                "state": "incoming",
            }
            for batch in sorted(
                incoming,
                key=lambda item: (
                    _aware(item.received_at) or datetime.min.replace(tzinfo=UTC),
                    int(item.id),
                ),
            )
        ]
        row = {
            "product_id": int(owner.id),
            "name": owner.name,
            "brand": owner.brand,
            "merchant_sku": primary_sku,
            "kaspi_product_id": owner.kaspi_product_id,
            "shared_merchant_skus": member_skus,
            "shared_kaspi_product_ids": member_kaspi_ids,
            "shared_cards_count": len(members),
            "on_hand_units": on_hand_units,
            "priced_on_hand_units": priced_on_hand_units,
            "unpriced_units": unpriced_units,
            "weighted_purchase_price": weighted_purchase_price,
            "last_purchase_price": None if latest is None else _money(Decimal(latest.unit_cost)),
            "inventory_value": _money(inventory_value),
            "current_batches_count": len(batches),
            "oldest_batch_at": min(received_dates) if received_dates else None,
            "last_purchase_at": None if latest is None else _aware(latest.received_at),
            "incoming_units": incoming_units,
            "incoming_priced_units": incoming_priced_units,
            "incoming_unpriced_units": incoming_units - incoming_priced_units,
            "incoming_value": _money(incoming_value),
            "batches": batch_rows,
            "incoming_batches": incoming_batch_rows,
        }
        if include_zero or on_hand_units > 0 or incoming_units > 0:
            rows.append(row)

    positive_rows = [row for row in rows if row["on_hand_units"] > 0]
    return {
        "workspace_id": workspace_id,
        "currency": "KZT",
        "catalog_positions": len(rows),
        "sku_count": len(positive_rows),
        "on_hand_units": sum(int(row["on_hand_units"]) for row in rows),
        "priced_on_hand_units": sum(int(row["priced_on_hand_units"]) for row in rows),
        "unpriced_units": sum(int(row["unpriced_units"]) for row in rows),
        "inventory_value": _money(sum((row["inventory_value"] for row in rows), Decimal("0"))),
        "incoming_units": sum(int(row["incoming_units"]) for row in rows),
        "incoming_unpriced_units": sum(int(row["incoming_unpriced_units"]) for row in rows),
        "incoming_value": _money(sum((row["incoming_value"] for row in rows), Decimal("0"))),
        "items": rows,
        "owner_by_product": owner_by_product,
        "products_by_id": products_by_id,
    }


def _line_cost(
    line: MarketplaceOrderLine,
    *,
    allocation_by_line: dict[int, tuple[int, Decimal]],
    owner_id: int | None,
    inventory_by_owner: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    quantity = max(int(line.quantity or 0), 0)
    allocated_quantity, allocated_cost = allocation_by_line.get(
        int(line.id), (0, Decimal("0"))
    )
    allocated_quantity = min(max(allocated_quantity, 0), quantity)
    fifo_cost = Decimal("0")
    if allocated_quantity > 0:
        raw_quantity = max(allocation_by_line[int(line.id)][0], 1)
        fifo_cost = allocated_cost * Decimal(allocated_quantity) / Decimal(raw_quantity)

    remaining = quantity - allocated_quantity
    inventory = inventory_by_owner.get(owner_id or -1, {})
    fallback_cost = inventory.get("weighted_purchase_price") or inventory.get("last_purchase_price")
    estimated_quantity = remaining if fallback_cost is not None else 0
    estimated_cost = Decimal(fallback_cost or 0) * estimated_quantity
    return {
        "quantity": quantity,
        "fifo_units": allocated_quantity,
        "estimated_units": estimated_quantity,
        "unpriced_units": remaining - estimated_quantity,
        "procurement_cost": _money(fifo_cost + estimated_cost),
    }


def _empty_product_stat(name: str, product_id: int | None) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "name": name,
        "orders": set(),
        "delivered_units": 0,
        "delivered_revenue": Decimal("0"),
        "cancelled_units": 0,
        "cancelled_demand": Decimal("0"),
        "returned_units": 0,
        "returned_value": Decimal("0"),
        "procurement_cost": Decimal("0"),
        "fifo_units": 0,
        "estimated_cost_units": 0,
        "unpriced_units": 0,
        "known_net_profit": Decimal("0"),
        "last_sale_at": None,
    }


def _summarize_period(
    orders: list[MarketplaceOrder],
    *,
    lines_by_order: dict[int, list[MarketplaceOrderLine]],
    allocation_by_line: dict[int, tuple[int, Decimal]],
    owner_by_product: dict[int, int],
    products_by_id: dict[int, Product],
    inventory_by_owner: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[int | str, dict[str, Any]]]:
    product_stats: dict[int | str, dict[str, Any]] = {}
    delivered_values: list[Decimal] = []
    delivered_orders = 0
    delivered_units = 0
    delivered_revenue = Decimal("0")
    cancelled_orders = 0
    cancelled_demand = Decimal("0")
    returned_orders = 0
    returned_value = Decimal("0")
    active_orders = 0
    active_demand = Decimal("0")
    procurement_cost = Decimal("0")
    fifo_units = 0
    estimated_cost_units = 0
    unpriced_units = 0
    kaspi_commission = Decimal("0")
    tax = Decimal("0")
    logistics = Decimal("0")
    partial_known_profit = Decimal("0")
    multi_unit_orders = 0
    multi_line_orders = 0
    all_units = 0

    for order in orders:
        status = str(order.status or "unknown").casefold()
        amount = Decimal(order.total_amount or 0)
        lines = lines_by_order.get(int(order.id), [])
        order_units = sum(max(int(line.quantity or 0), 0) for line in lines)
        all_units += order_units

        if status == _DELIVERED:
            delivered_orders += 1
            delivered_units += order_units
            delivered_revenue += amount
            delivered_values.append(amount)
            if order_units > 1:
                multi_unit_orders += 1
            if len(lines) > 1:
                multi_line_orders += 1
            kaspi_commission += amount * KASPI_COMMISSION_RATE
            tax += amount * TAX_RATE
            order_logistics = kaspi_logistics_per_unit(amount)
            logistics += order_logistics
            logistics_shares = allocate_order_logistics(
                order_total=amount,
                line_totals=tuple(Decimal(line.line_total or 0) for line in lines),
            )
        else:
            logistics_shares = tuple(Decimal("0") for _line in lines)
            if status in _CANCELLED:
                cancelled_orders += 1
                cancelled_demand += amount
            elif status == _RETURNED:
                returned_orders += 1
                returned_value += amount
            else:
                active_orders += 1
                active_demand += amount

        for index, line in enumerate(lines):
            product_id = int(line.product_id) if line.product_id is not None else None
            owner_id = owner_by_product.get(product_id, product_id) if product_id is not None else None
            product = products_by_id.get(owner_id or -1)
            identity = (
                owner_id
                if owner_id is not None
                else f"unresolved:{line.external_product_id or line.merchant_sku or line.title}"
            )
            name = product.name if product is not None else line.title
            stat = product_stats.setdefault(identity, _empty_product_stat(name, owner_id))
            quantity = max(int(line.quantity or 0), 0)
            line_total = Decimal(line.line_total or 0)

            if status in _CANCELLED:
                stat["cancelled_units"] += quantity
                stat["cancelled_demand"] += line_total
                continue
            if status == _RETURNED:
                stat["returned_units"] += quantity
                stat["returned_value"] += line_total
                continue
            if status != _DELIVERED:
                continue

            stat["orders"].add(int(order.id))
            stat["delivered_units"] += quantity
            stat["delivered_revenue"] += line_total
            sale_at = _aware(order.delivered_at or order.ordered_at)
            if sale_at is not None and (
                stat["last_sale_at"] is None or sale_at > stat["last_sale_at"]
            ):
                stat["last_sale_at"] = sale_at

            cost = _line_cost(
                line,
                allocation_by_line=allocation_by_line,
                owner_id=owner_id,
                inventory_by_owner=inventory_by_owner,
            )
            procurement_cost += cost["procurement_cost"]
            fifo_units += int(cost["fifo_units"])
            estimated_cost_units += int(cost["estimated_units"])
            unpriced_units += int(cost["unpriced_units"])
            stat["procurement_cost"] += cost["procurement_cost"]
            stat["fifo_units"] += int(cost["fifo_units"])
            stat["estimated_cost_units"] += int(cost["estimated_units"])
            stat["unpriced_units"] += int(cost["unpriced_units"])

            priced_quantity = int(cost["fifo_units"]) + int(cost["estimated_units"])
            if quantity > 0 and priced_quantity > 0:
                ratio = Decimal(priced_quantity) / Decimal(quantity)
                known_revenue = line_total * ratio
                known_logistics = (logistics_shares[index] if index < len(logistics_shares) else Decimal("0")) * ratio
                known_profit = (
                    known_revenue
                    - cost["procurement_cost"]
                    - known_revenue * KASPI_COMMISSION_RATE
                    - known_revenue * TAX_RATE
                    - known_logistics
                )
                stat["known_net_profit"] += known_profit
                partial_known_profit += known_profit

    procurement_cost = _money(procurement_cost)
    kaspi_commission = _money(kaspi_commission)
    tax = _money(tax)
    logistics = _money(logistics)
    is_complete = unpriced_units == 0
    complete_profit = _money(
        delivered_revenue - procurement_cost - kaspi_commission - tax - logistics
    ) if is_complete else None
    known_profit = complete_profit if complete_profit is not None else _money(partial_known_profit)
    profit_base = complete_profit if complete_profit is not None else known_profit

    return {
        "orders_total": len(orders),
        "units_total": all_units,
        "active_orders": active_orders,
        "active_demand": _money(active_demand),
        "delivered_orders": delivered_orders,
        "delivered_units": delivered_units,
        "delivered_revenue": _money(delivered_revenue),
        "cancelled_orders": cancelled_orders,
        "cancelled_demand": _money(cancelled_demand),
        "returned_orders": returned_orders,
        "returned_value": _money(returned_value),
        "average_delivered_order": _money(delivered_revenue / delivered_orders) if delivered_orders else Decimal("0.00"),
        "median_delivered_order": _money(median(delivered_values)) if delivered_values else Decimal("0.00"),
        "units_per_delivered_order": _percent(Decimal(delivered_units) / delivered_orders) if delivered_orders else Decimal("0.00"),
        "multi_unit_orders_share_pct": _percent(Decimal(multi_unit_orders) * 100 / delivered_orders) if delivered_orders else Decimal("0.00"),
        "multi_line_orders_share_pct": _percent(Decimal(multi_line_orders) * 100 / delivered_orders) if delivered_orders else Decimal("0.00"),
        "cancellation_rate_pct": _percent(Decimal(cancelled_orders) * 100 / len(orders)) if orders else Decimal("0.00"),
        "return_rate_pct": _percent(Decimal(returned_orders) * 100 / len(orders)) if orders else Decimal("0.00"),
        "procurement_cost": procurement_cost,
        "kaspi_commission": kaspi_commission,
        "tax": tax,
        "logistics": logistics,
        "net_profit": complete_profit,
        "known_net_profit": known_profit,
        "net_margin_pct": _percent(profit_base * 100 / delivered_revenue) if delivered_revenue > 0 else Decimal("0.00"),
        "result_is_complete": is_complete,
        "fifo_cost_units": fifo_units,
        "estimated_cost_units": estimated_cost_units,
        "unpriced_units": unpriced_units,
        "cost_coverage_pct": _percent(Decimal(delivered_units - unpriced_units) * 100 / delivered_units) if delivered_units else Decimal("100.00"),
    }, product_stats


def _last_sales_by_owner(
    db: Session,
    *,
    workspace_id: int,
    owner_by_product: dict[int, int],
) -> dict[int, datetime]:
    rows = db.execute(
        select(
            MarketplaceOrderLine.product_id,
            func.max(func.coalesce(MarketplaceOrder.delivered_at, MarketplaceOrder.ordered_at)),
        )
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .where(
            MarketplaceOrder.workspace_id == workspace_id,
            MarketplaceOrder.status == _DELIVERED,
            MarketplaceOrderLine.product_id.is_not(None),
        )
        .group_by(MarketplaceOrderLine.product_id)
    ).all()
    result: dict[int, datetime] = {}
    for product_id, sold_at in rows:
        if sold_at is None:
            continue
        owner_id = owner_by_product.get(int(product_id), int(product_id))
        aware = _aware(sold_at)
        if aware is not None and (owner_id not in result or aware > result[owner_id]):
            result[owner_id] = aware
    return result


def _product_rows(
    *,
    current: dict[int | str, dict[str, Any]],
    previous: dict[int | str, dict[str, Any]],
    inventory: dict[str, Any],
    period_days: int,
    last_sales: dict[int, datetime],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    inventory_by_owner = {int(row["product_id"]): row for row in inventory["items"]}
    keys = set(current) | set(previous) | {
        owner_id
        for owner_id, row in inventory_by_owner.items()
        if int(row["on_hand_units"]) > 0 or int(row["incoming_units"]) > 0
    }
    total_revenue = sum(
        (Decimal(current.get(key, {}).get("delivered_revenue", 0)) for key in keys),
        Decimal("0"),
    )
    ordered_keys = sorted(
        keys,
        key=lambda key: (
            -Decimal(current.get(key, {}).get("delivered_revenue", 0)),
            -Decimal(inventory_by_owner.get(key, {}).get("inventory_value", 0)) if isinstance(key, int) else Decimal("0"),
            str(current.get(key, previous.get(key, {})).get("name", "")),
        ),
    )

    rows: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    for rank, key in enumerate(ordered_keys, start=1):
        current_stat = current.get(key, {})
        previous_stat = previous.get(key, {})
        product_id = current_stat.get("product_id", previous_stat.get("product_id"))
        if product_id is None and isinstance(key, int):
            product_id = key
        inv = inventory_by_owner.get(int(product_id), {}) if product_id is not None else {}
        revenue = Decimal(current_stat.get("delivered_revenue", 0))
        previous_revenue = Decimal(previous_stat.get("delivered_revenue", 0))
        share = revenue * Decimal("100") / total_revenue if total_revenue > 0 else Decimal("0")
        before = cumulative
        cumulative += share
        abc_class = "A" if before < 80 and revenue > 0 else "B" if before < 95 and revenue > 0 else "C"
        delivered_units = int(current_stat.get("delivered_units", 0))
        on_hand = int(inv.get("on_hand_units", 0))
        daily_velocity = Decimal(delivered_units) / Decimal(max(period_days, 1))
        days_cover = (
            _percent(Decimal(on_hand) / daily_velocity)
            if daily_velocity > 0
            else None
        )
        trend = _change_pct(revenue, previous_revenue)
        inventory_value = Decimal(inv.get("inventory_value", 0))

        if previous_revenue > 0 and revenue == 0:
            flag = "lost_sales"
            flag_label = "Пропал из продаж"
        elif delivered_units > 0 and on_hand == 0:
            flag = "out_of_stock"
            flag_label = "Нет физического остатка"
        elif days_cover is not None and days_cover <= 7:
            flag = "low_stock"
            flag_label = "Запас не более 7 дней"
        elif revenue == 0 and inventory_value > 0:
            flag = "frozen_capital"
            flag_label = "Капитал без продаж"
        elif trend is not None and trend >= 50 and revenue > 0:
            flag = "accelerating"
            flag_label = "Продажи разгоняются"
        elif previous_revenue == 0 and revenue > 0:
            flag = "new_demand"
            flag_label = "Новый спрос"
        else:
            flag = "stable"
            flag_label = "Без критичного сигнала"

        row_unpriced = int(current_stat.get("unpriced_units", 0))
        known_profit = _money(Decimal(current_stat.get("known_net_profit", 0)))
        rows.append(
            {
                "rank": rank,
                "product_id": product_id,
                "name": current_stat.get("name") or previous_stat.get("name") or inv.get("name") or "Неизвестный товар",
                "brand": inv.get("brand"),
                "merchant_sku": inv.get("merchant_sku"),
                "kaspi_product_id": inv.get("kaspi_product_id"),
                "shared_merchant_skus": inv.get("shared_merchant_skus", []),
                "abc_class": abc_class,
                "revenue_share_pct": _percent(share),
                "cumulative_revenue_share_pct": _percent(cumulative),
                "orders_count": len(current_stat.get("orders", set())),
                "delivered_units": delivered_units,
                "delivered_revenue": _money(revenue),
                "previous_revenue": _money(previous_revenue),
                "revenue_change_pct": trend,
                "cancelled_units": int(current_stat.get("cancelled_units", 0)),
                "cancelled_demand": _money(Decimal(current_stat.get("cancelled_demand", 0))),
                "returned_units": int(current_stat.get("returned_units", 0)),
                "returned_value": _money(Decimal(current_stat.get("returned_value", 0))),
                "procurement_cost": _money(Decimal(current_stat.get("procurement_cost", 0))),
                "known_net_profit": known_profit,
                "result_is_complete": row_unpriced == 0,
                "fifo_cost_units": int(current_stat.get("fifo_units", 0)),
                "estimated_cost_units": int(current_stat.get("estimated_cost_units", 0)),
                "unpriced_units": row_unpriced,
                "on_hand_units": on_hand,
                "incoming_units": int(inv.get("incoming_units", 0)),
                "weighted_purchase_price": inv.get("weighted_purchase_price"),
                "last_purchase_price": inv.get("last_purchase_price"),
                "inventory_value": _money(inventory_value),
                "days_of_stock": days_cover,
                "last_sale_at": last_sales.get(int(product_id)) if product_id is not None else current_stat.get("last_sale_at"),
                "signal": flag,
                "signal_label": flag_label,
            }
        )

    groups: dict[str, dict[str, Any]] = {
        letter: {"class": letter, "products_count": 0, "revenue": Decimal("0")}
        for letter in ("A", "B", "C")
    }
    brands: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = groups[row["abc_class"]]
        group["products_count"] += 1
        group["revenue"] += row["delivered_revenue"]
        brand = (row.get("brand") or "Без бренда").strip()
        brand_row = brands.setdefault(
            brand,
            {"brand": brand, "products_count": 0, "revenue": Decimal("0")},
        )
        brand_row["products_count"] += 1
        brand_row["revenue"] += row["delivered_revenue"]

    for group in groups.values():
        group["revenue"] = _money(group["revenue"])
        group["share_pct"] = _percent(
            group["revenue"] * Decimal("100") / total_revenue
        ) if total_revenue > 0 else Decimal("0.00")

    brand_rows = sorted(brands.values(), key=lambda item: item["revenue"], reverse=True)
    for brand in brand_rows:
        brand["revenue"] = _money(brand["revenue"])
        brand["share_pct"] = _percent(
            brand["revenue"] * Decimal("100") / total_revenue
        ) if total_revenue > 0 else Decimal("0.00")

    return rows, {"groups": [groups[letter] for letter in ("A", "B", "C")]}, brand_rows[:8]


def build_accounting_report(
    db: Session,
    *,
    workspace_id: int,
    days: int = 30,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return management accounting analytics without mutating operational tables."""

    observed_at = _aware(as_of) or datetime.now(UTC)
    current_start = observed_at - timedelta(days=days) if days > 0 else None
    previous_start = observed_at - timedelta(days=days * 2) if days > 0 else None
    query_start = previous_start if previous_start is not None else None

    order_query = select(MarketplaceOrder).where(
        MarketplaceOrder.workspace_id == workspace_id,
        MarketplaceOrder.ordered_at.is_not(None),
        MarketplaceOrder.ordered_at <= observed_at,
    )
    if query_start is not None:
        order_query = order_query.where(MarketplaceOrder.ordered_at >= query_start)
    orders = list(db.scalars(order_query.order_by(MarketplaceOrder.ordered_at, MarketplaceOrder.id)).all())
    order_ids = [int(order.id) for order in orders]

    lines = list(
        db.scalars(
            select(MarketplaceOrderLine)
            .where(MarketplaceOrderLine.marketplace_order_id.in_(order_ids))
            .order_by(MarketplaceOrderLine.marketplace_order_id, MarketplaceOrderLine.id)
        ).all()
    ) if order_ids else []
    lines_by_order: dict[int, list[MarketplaceOrderLine]] = defaultdict(list)
    for line in lines:
        lines_by_order[int(line.marketplace_order_id)].append(line)

    line_ids = [int(line.id) for line in lines]
    allocation_by_line = {
        int(line_id): (int(quantity or 0), Decimal(total_cost or 0))
        for line_id, quantity, total_cost in (
            db.execute(
                select(
                    InventoryAllocation.marketplace_order_line_id,
                    func.sum(InventoryAllocation.quantity),
                    func.sum(InventoryAllocation.quantity * InventoryAllocation.unit_cost),
                )
                .where(InventoryAllocation.marketplace_order_line_id.in_(line_ids))
                .group_by(InventoryAllocation.marketplace_order_line_id)
            ).all()
            if line_ids
            else []
        )
    }

    inventory = build_inventory_snapshot(
        db,
        workspace_id=workspace_id,
        include_zero=True,
    )
    owner_by_product: dict[int, int] = inventory.pop("owner_by_product")
    products_by_id: dict[int, Product] = inventory.pop("products_by_id")
    inventory_by_owner = {
        int(row["product_id"]): row for row in inventory["items"]
    }

    if current_start is None:
        current_orders = orders
        previous_orders: list[MarketplaceOrder] = []
        dated_orders = [_aware(order.ordered_at) for order in orders if order.ordered_at]
        period_days = max((observed_at - min(dated_orders)).days + 1, 1) if dated_orders else 1
    else:
        current_orders = [
            order for order in orders
            if (_aware(order.ordered_at) or observed_at) >= current_start
        ]
        previous_orders = [
            order for order in orders
            if previous_start is not None
            and previous_start <= (_aware(order.ordered_at) or observed_at) < current_start
        ]
        period_days = days

    current_summary, current_products = _summarize_period(
        current_orders,
        lines_by_order=lines_by_order,
        allocation_by_line=allocation_by_line,
        owner_by_product=owner_by_product,
        products_by_id=products_by_id,
        inventory_by_owner=inventory_by_owner,
    )
    previous_summary, previous_products = _summarize_period(
        previous_orders,
        lines_by_order=lines_by_order,
        allocation_by_line=allocation_by_line,
        owner_by_product=owner_by_product,
        products_by_id=products_by_id,
        inventory_by_owner=inventory_by_owner,
    )
    last_sales = _last_sales_by_owner(
        db,
        workspace_id=workspace_id,
        owner_by_product=owner_by_product,
    )
    product_rows, abc, brands = _product_rows(
        current=current_products,
        previous=previous_products,
        inventory=inventory,
        period_days=period_days,
        last_sales=last_sales,
    )

    comparison = None
    if days > 0:
        comparison = {
            "delivered_revenue_change_pct": _change_pct(
                current_summary["delivered_revenue"], previous_summary["delivered_revenue"]
            ),
            "delivered_orders_change_pct": _change_pct(
                current_summary["delivered_orders"], previous_summary["delivered_orders"]
            ),
            "net_profit_change_pct": _change_pct(
                current_summary["known_net_profit"], previous_summary["known_net_profit"]
            ),
            "previous": previous_summary,
        }

    signals = {
        signal: [row for row in product_rows if row["signal"] == signal][:12]
        for signal in (
            "lost_sales",
            "out_of_stock",
            "low_stock",
            "frozen_capital",
            "accelerating",
            "new_demand",
        )
    }
    return {
        "workspace_id": workspace_id,
        "currency": "KZT",
        "generated_at": observed_at,
        "period": {
            "days": days,
            "start": current_start,
            "end": observed_at,
            "previous_start": previous_start,
            "previous_end": current_start,
        },
        "calculation": {
            "kind": "management_accounting",
            "commission_rate_pct": _percent(KASPI_COMMISSION_RATE * 100),
            "tax_rate_pct": _percent(TAX_RATE * 100),
            "cost_policy": "FIFO allocation; current weighted purchase price; last purchase price",
            "read_only": True,
        },
        "summary": current_summary,
        "comparison": comparison,
        "inventory": inventory,
        "abc": abc,
        "brands": brands,
        "signals": signals,
        "products": product_rows,
    }
