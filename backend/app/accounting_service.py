from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .commerce.profit_calculator import (
    KASPI_COMMISSION_RATE,
    TAX_RATE,
    allocate_order_logistics,
    kaspi_logistics_per_unit,
)
from .accounting_models import AccountingCapitalSnapshot
from .inventory_models import InventoryAllocation, InventoryBatch, InventoryBatchType
from .models import MarketplaceOrder, MarketplaceOrderEvent, MarketplaceOrderLine, Product
from .workspace_models import Workspace


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


def _accounting_date_expression():
    first_delivery = (
        select(func.min(MarketplaceOrderEvent.occurred_at))
        .where(
            MarketplaceOrderEvent.marketplace_order_id == MarketplaceOrder.id,
            MarketplaceOrderEvent.current_status == _DELIVERED,
        )
        .correlate(MarketplaceOrder)
        .scalar_subquery()
    )
    return case(
        (MarketplaceOrder.status == _DELIVERED, func.coalesce(
            MarketplaceOrder.delivered_at, first_delivery,
            MarketplaceOrder.source_updated_at, MarketplaceOrder.ordered_at,
            MarketplaceOrder.created_at,
        )),
        else_=func.coalesce(MarketplaceOrder.ordered_at, MarketplaceOrder.created_at),
    )


def _order_cost_data(db: Session, orders: list[MarketplaceOrder]):
    ids = [int(order.id) for order in orders]
    lines = list(db.scalars(select(MarketplaceOrderLine).where(
        MarketplaceOrderLine.marketplace_order_id.in_(ids)
    )).all()) if ids else []
    by_order = defaultdict(list)
    for line in lines:
        by_order[int(line.marketplace_order_id)].append(line)
    line_ids = [int(line.id) for line in lines]
    allocations = {
        int(line_id): (int(quantity or 0), Decimal(cost or 0))
        for line_id, quantity, cost in (db.execute(
            select(InventoryAllocation.marketplace_order_line_id,
                   func.sum(InventoryAllocation.quantity),
                   func.sum(InventoryAllocation.quantity * InventoryAllocation.unit_cost))
            .where(InventoryAllocation.marketplace_order_line_id.in_(line_ids))
            .group_by(InventoryAllocation.marketplace_order_line_id)
        ).all() if line_ids else [])
    }
    return by_order, allocations



def _historical_line_costs(db: Session, *, workspace_id: int, orders, lines_by_order, owner_by_product):
    histories = defaultdict(list)
    batches = db.scalars(select(InventoryBatch).where(
        InventoryBatch.workspace_id == workspace_id,
        InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
        InventoryBatch.is_received.is_(True),
    )).all()
    for batch in batches:
        owner = owner_by_product.get(int(batch.product_id), int(batch.product_id))
        histories[owner].append((_aware(batch.received_at), int(batch.id), Decimal(batch.unit_cost)))
    for history in histories.values():
        history.sort()
    dates = dict(db.execute(select(MarketplaceOrder.id, _accounting_date_expression()).where(
        MarketplaceOrder.id.in_([int(order.id) for order in orders])
    )).all()) if orders else {}
    costs = {}
    for order in orders:
        at = _aware(dates.get(int(order.id)))
        for line in lines_by_order.get(int(order.id), []):
            owner = owner_by_product.get(int(line.product_id or 0), int(line.product_id or 0))
            history = histories.get(owner, [])
            index = bisect_right(history, (at, float("inf"), Decimal("0"))) - 1 if at is not None else -1
            costs[int(line.id)] = history[index][2] if index >= 0 else None
    return costs

def _capital_totals(db: Session, *, workspace_id: int, before: datetime | None = None):
    """Cumulative recorded operations; recomputation also reverses returned sales."""
    inventory = build_inventory_snapshot(db, workspace_id=workspace_id)
    query = select(MarketplaceOrder).where(
        MarketplaceOrder.workspace_id == workspace_id,
        MarketplaceOrder.status == _DELIVERED,
    )
    if before is not None:
        query = query.where(_accounting_date_expression() <= before)
    orders = list(db.scalars(query).all())
    lines, allocations = _order_cost_data(db, orders)
    summary, _ = _summarize_period(
        orders, lines_by_order=lines, allocation_by_line=allocations,
        fallback_cost_by_line=_historical_line_costs(db, workspace_id=workspace_id, orders=orders,
            lines_by_order=lines, owner_by_product=inventory["owner_by_product"]),
        owner_by_product=inventory["owner_by_product"],
        products_by_id=inventory["products_by_id"],
        inventory_by_owner={int(row["product_id"]): row for row in inventory["items"]},
    )
    purchase_query = select(func.sum(InventoryBatch.quantity_received * InventoryBatch.unit_cost)).where(
        InventoryBatch.workspace_id == workspace_id,
        InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
    )
    if before is not None:
        purchase_query = purchase_query.where(InventoryBatch.created_at <= before)
    purchases = _money(Decimal(db.scalar(purchase_query) or 0))
    receipts = _money(summary["delivered_revenue"] - summary["kaspi_commission"]
                      - summary["tax"] - summary["logistics"])
    return receipts, summary["known_net_profit"], purchases, summary["result_is_complete"]


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


def record_capital_snapshot(
    db: Session,
    *,
    workspace_id: int,
    cash_balance_kzt: Decimal,
    free_capital_kzt: Decimal,
    note: str | None = None,
) -> AccountingCapitalSnapshot:
    """Append a manual cash position without modifying earlier snapshots."""

    cash = _money(cash_balance_kzt)
    free = _money(free_capital_kzt)
    if cash < 0 or free < 0:
        raise ValueError("capital values must be non-negative")
    if free > cash:
        raise ValueError("free capital cannot exceed cash balance")
    snapshot = AccountingCapitalSnapshot(
        workspace_id=workspace_id,
        cash_balance_kzt=cash,
        free_capital_kzt=free,
        note=(note or "").strip() or None,
    )
    receipts, profit, purchases, _ = _capital_totals(db, workspace_id=workspace_id)
    snapshot.sales_receipts_kzt = receipts
    snapshot.sales_profit_kzt = profit
    snapshot.purchases_kzt = purchases
    db.add(snapshot)
    db.flush()
    return snapshot


def build_capital_position(
    db: Session,
    *,
    workspace_id: int,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Opening balance plus cumulative sales and paid purchase changes."""

    latest = db.scalar(
        select(AccountingCapitalSnapshot)
        .where(AccountingCapitalSnapshot.workspace_id == workspace_id)
        .order_by(
            AccountingCapitalSnapshot.created_at.desc(),
            AccountingCapitalSnapshot.id.desc(),
        )
        .limit(1)
    )
    workspace_name = db.scalar(
        select(Workspace.name).where(Workspace.id == workspace_id)
    ) or f"Workspace {workspace_id}"
    warehouse_value = _money(Decimal(inventory.get("inventory_value") or 0))
    incoming_value = _money(Decimal(inventory.get("incoming_value") or 0))
    receipts, profit, purchases, profit_complete = _capital_totals(db, workspace_id=workspace_id)
    legacy_baseline = latest is not None and latest.sales_receipts_kzt is None
    if latest is None:
        baseline_receipts = baseline_profit = baseline_purchases = Decimal("0")
    elif legacy_baseline:
        baseline_receipts, baseline_profit, baseline_purchases, _ = _capital_totals(
            db, workspace_id=workspace_id, before=_aware(latest.created_at),
        )
    else:
        baseline_receipts = Decimal(latest.sales_receipts_kzt)
        baseline_profit = Decimal(latest.sales_profit_kzt)
        baseline_purchases = Decimal(latest.purchases_kzt)
    receipts_change = _money(receipts - baseline_receipts)
    purchases_change = _money(purchases - baseline_purchases)
    profit_change = _money(profit - baseline_profit)
    cash_balance = _money(Decimal(latest.cash_balance_kzt if latest else 0)
                          + receipts_change - purchases_change)
    accumulated_profit = _money(Decimal(latest.free_capital_kzt if latest else 0) + profit_change)
    free_capital = _money(min(max(cash_balance, Decimal("0")),
                             max(accumulated_profit, Decimal("0")))) if profit_complete else None
    unpriced_warehouse_units = int(inventory.get("unpriced_units") or 0)
    unpriced_incoming_units = int(inventory.get("incoming_unpriced_units") or 0)
    valuation_is_complete = (
        unpriced_warehouse_units == 0 and unpriced_incoming_units == 0
    )
    known_total = _money(
        warehouse_value + incoming_value + Decimal(cash_balance or 0)
    )
    total_capital = known_total if valuation_is_complete else None
    return {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "currency": "KZT",
        "cash_is_configured": latest is not None,
        "cash_balance": cash_balance,
        "cash_is_estimated": True,
        "opening_balance_is_configured": latest is not None,
        "legacy_baseline_is_estimated": legacy_baseline,
        "sales_net_receipts": receipts,
        "sales_net_profit": profit if profit_complete else None,
        "sales_profit_is_complete": profit_complete,
        "sales_receipts_change": receipts_change,
        "sales_profit_change": profit_change if profit_complete else None,
        "paid_purchases_change": purchases_change,
        "accumulated_profit": accumulated_profit if profit_complete else None,
        "warehouse_at_cost": warehouse_value,
        "goods_in_transit": incoming_value,
        "free_capital": free_capital,
        "known_total_capital": known_total,
        "total_capital": total_capital,
        "valuation_is_complete": valuation_is_complete,
        "unpriced_warehouse_units": unpriced_warehouse_units,
        "unpriced_incoming_units": unpriced_incoming_units,
        "snapshot_id": None if latest is None else int(latest.id),
        "snapshot_created_at": None if latest is None else _aware(latest.created_at),
        "snapshot_note": None if latest is None else latest.note,
        "formula": "cash_balance + warehouse_at_cost + goods_in_transit",
        "free_capital_is_part_of_cash": True,
    }


def _line_cost(
    line: MarketplaceOrderLine,
    *,
    allocation_by_line: dict[int, tuple[int, Decimal]],
    owner_id: int | None,
    inventory_by_owner: dict[int, dict[str, Any]],
    fallback_cost_by_line: dict[int, Decimal | None],
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
    fallback_cost = fallback_cost_by_line.get(int(line.id))
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
    fallback_cost_by_line: dict[int, Decimal | None],
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
    missing_cost_orders = 0
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
        if status == _DELIVERED and not lines:
            missing_cost_orders += 1
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
                fallback_cost_by_line=fallback_cost_by_line,
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
    is_complete = unpriced_units == 0 and missing_cost_orders == 0
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
        "missing_cost_orders": missing_cost_orders,
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
            func.max(_accounting_date_expression()),
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

    accounting_date = _accounting_date_expression()
    order_query = select(MarketplaceOrder, accounting_date.label("accounting_at")).where(
        MarketplaceOrder.workspace_id == workspace_id,
        accounting_date <= observed_at,
    )
    if query_start is not None:
        order_query = order_query.where(accounting_date >= query_start)
    dated_rows = db.execute(order_query.order_by(accounting_date, MarketplaceOrder.id)).all()
    orders = [order for order, _ in dated_rows]
    dates = {int(order.id): _aware(at) for order, at in dated_rows}
    lines_by_order, allocation_by_line = _order_cost_data(db, orders)

    inventory = build_inventory_snapshot(
        db,
        workspace_id=workspace_id,
        include_zero=True,
    )
    owner_by_product: dict[int, int] = inventory.pop("owner_by_product")
    products_by_id: dict[int, Product] = inventory.pop("products_by_id")
    capital = build_capital_position(
        db,
        workspace_id=workspace_id,
        inventory=inventory,
    )
    inventory_by_owner = {
        int(row["product_id"]): row for row in inventory["items"]
    }

    if current_start is None:
        current_orders = orders
        previous_orders: list[MarketplaceOrder] = []
        dated_orders = list(dates.values())
        period_days = max((observed_at - min(dated_orders)).days + 1, 1) if dated_orders else 1
    else:
        current_orders = [
            order for order in orders
            if (dates[int(order.id)] or observed_at) >= current_start
        ]
        previous_orders = [
            order for order in orders
            if previous_start is not None
            and previous_start <= (dates[int(order.id)] or observed_at) < current_start
        ]
        period_days = days

    fallback_cost_by_line = _historical_line_costs(db, workspace_id=workspace_id, orders=orders,
        lines_by_order=lines_by_order, owner_by_product=owner_by_product)
    current_summary, current_products = _summarize_period(
        current_orders,
        lines_by_order=lines_by_order,
        allocation_by_line=allocation_by_line,
        owner_by_product=owner_by_product,
        products_by_id=products_by_id,
        inventory_by_owner=inventory_by_owner,
        fallback_cost_by_line=fallback_cost_by_line,
    )
    previous_summary, previous_products = _summarize_period(
        previous_orders,
        lines_by_order=lines_by_order,
        allocation_by_line=allocation_by_line,
        owner_by_product=owner_by_product,
        products_by_id=products_by_id,
        inventory_by_owner=inventory_by_owner,
        fallback_cost_by_line=fallback_cost_by_line,
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
            "cost_policy": "FIFO allocation; last received purchase price on or before the sale",
            "read_only": True,
            "sales_date_policy": "delivery date; first delivered event; source update; creation fallback",
            "cash_policy": "estimated net delivered receipts minus recorded paid purchases; optional opening balance",
        },
        "summary": current_summary,
        "capital": capital,
        "comparison": comparison,
        "inventory": inventory,
        "abc": abc,
        "brands": brands,
        "signals": signals,
        "products": product_rows,
    }
