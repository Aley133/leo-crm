from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .inventory_models import InventoryAllocation, InventoryBatch
from .models import MarketplaceOrder, MarketplaceOrderLine, MarketplaceOrderStatus, Product


_TERMINAL_ORDER_STATUSES = {
    MarketplaceOrderStatus.CANCELLING.value,
    MarketplaceOrderStatus.CANCELLED.value,
    MarketplaceOrderStatus.RETURNED.value,
}


@dataclass(frozen=True, slots=True)
class AllocationResult:
    requested_quantity: int
    previously_allocated_quantity: int
    newly_allocated_quantity: int

    @property
    def allocated_quantity(self) -> int:
        return self.previously_allocated_quantity + self.newly_allocated_quantity

    @property
    def remaining_quantity(self) -> int:
        return max(self.requested_quantity - self.allocated_quantity, 0)

    @property
    def fully_allocated(self) -> bool:
        return self.remaining_quantity == 0


def _almaty_day_end(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local_date = aware.astimezone(ZoneInfo("Asia/Almaty")).date()
    return datetime.combine(local_date, time.max, tzinfo=ZoneInfo("Asia/Almaty")).astimezone(UTC)


def _almaty_day_start(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local_date = aware.astimezone(ZoneInfo("Asia/Almaty")).date()
    return datetime.combine(local_date, time.min, tzinfo=ZoneInfo("Asia/Almaty")).astimezone(UTC)


def allocated_quantity_for_line(session: Session, order_line_id: int) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(InventoryAllocation.quantity), 0)).where(
                InventoryAllocation.marketplace_order_line_id == order_line_id
            )
        )
        or 0
    )


def allocate_order_line_fifo(
    session: Session,
    *,
    order_line: MarketplaceOrderLine,
    order: MarketplaceOrder | None = None,
    allocated_at: datetime | None = None,
) -> AllocationResult:
    """Allocate available inventory to one order line without double write-off.

    FIFO is based on batch receipt time. A batch is eligible when it was received
    no later than the end of the order's local Almaty calendar day. This matches
    the owner's operational rule: stock received on 23 July may cover orders from
    23 July, regardless of the exact clock time used during manual entry.
    """

    requested = max(int(order_line.quantity or 0), 0)
    previous = allocated_quantity_for_line(session, order_line.id)
    needed = max(requested - previous, 0)
    if needed == 0 or order_line.product_id is None:
        return AllocationResult(requested, previous, 0)

    resolved_order = order or session.get(MarketplaceOrder, order_line.marketplace_order_id)
    if resolved_order is None or resolved_order.status in _TERMINAL_ORDER_STATUSES:
        return AllocationResult(requested, previous, 0)

    order_time = resolved_order.ordered_at or allocated_at or datetime.now(UTC)
    eligible_until = _almaty_day_end(order_time)
    now = allocated_at or datetime.now(UTC)

    batches = session.scalars(
        select(InventoryBatch)
        .where(
            InventoryBatch.product_id == order_line.product_id,
            InventoryBatch.quantity_remaining > 0,
            InventoryBatch.received_at <= eligible_until,
        )
        .order_by(InventoryBatch.received_at, InventoryBatch.id)
        .with_for_update()
    ).all()

    newly_allocated = 0
    for batch in batches:
        if needed <= 0:
            break
        quantity = min(needed, int(batch.quantity_remaining))
        if quantity <= 0:
            continue

        existing = session.scalar(
            select(InventoryAllocation).where(
                InventoryAllocation.inventory_batch_id == batch.id,
                InventoryAllocation.marketplace_order_line_id == order_line.id,
            )
        )
        if existing is None:
            session.add(
                InventoryAllocation(
                    inventory_batch_id=batch.id,
                    marketplace_order_line_id=order_line.id,
                    quantity=quantity,
                    unit_cost=Decimal(batch.unit_cost),
                    allocated_at=now,
                )
            )
        else:
            existing.quantity += quantity

        batch.quantity_remaining -= quantity
        needed -= quantity
        newly_allocated += quantity

    session.flush()
    return AllocationResult(requested, previous, newly_allocated)


def reconcile_product_orders_from_batch(
    session: Session,
    *,
    batch: InventoryBatch,
    allocated_at: datetime | None = None,
) -> int:
    """Use a newly received batch for active orders from its Almaty receipt date."""

    day_start = _almaty_day_start(batch.received_at)
    rows = session.execute(
        select(MarketplaceOrderLine, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .where(
            MarketplaceOrderLine.product_id == batch.product_id,
            MarketplaceOrder.ordered_at >= day_start,
            MarketplaceOrder.status.not_in(_TERMINAL_ORDER_STATUSES),
        )
        .order_by(MarketplaceOrder.ordered_at, MarketplaceOrder.id, MarketplaceOrderLine.id)
    ).all()

    allocated = 0
    for line, order in rows:
        result = allocate_order_line_fifo(
            session,
            order_line=line,
            order=order,
            allocated_at=allocated_at,
        )
        allocated += result.newly_allocated_quantity
        if batch.quantity_remaining <= 0:
            break
    return allocated


def rebuild_product_fifo(
    session: Session,
    *,
    product_id: int,
    allocated_at: datetime | None = None,
) -> int:
    """Rebuild every FIFO allocation for one product after a stock correction.

    This is the authoritative correction path for editing or deleting a batch.
    Existing allocations are removed, all batch balances are reset, and eligible
    order lines are allocated again in chronological order. No stale cost remains
    attached to an order after the source batch changes.
    """

    batch_ids = select(InventoryBatch.id).where(InventoryBatch.product_id == product_id)
    session.execute(
        delete(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id.in_(batch_ids)
        )
    )

    batches = session.scalars(
        select(InventoryBatch)
        .where(InventoryBatch.product_id == product_id)
        .order_by(InventoryBatch.received_at, InventoryBatch.id)
        .with_for_update()
    ).all()
    for batch in batches:
        batch.quantity_remaining = batch.quantity_received
    session.flush()

    rows = session.execute(
        select(MarketplaceOrderLine, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .where(
            MarketplaceOrderLine.product_id == product_id,
            MarketplaceOrder.status.not_in(_TERMINAL_ORDER_STATUSES),
        )
        .order_by(MarketplaceOrder.ordered_at, MarketplaceOrder.id, MarketplaceOrderLine.id)
    ).all()

    allocated = 0
    now = allocated_at or datetime.now(UTC)
    for line, order in rows:
        result = allocate_order_line_fifo(
            session,
            order_line=line,
            order=order,
            allocated_at=now,
        )
        allocated += result.newly_allocated_quantity
    session.flush()
    return allocated


def create_inventory_batch(
    session: Session,
    *,
    product: Product,
    quantity: int,
    unit_cost: Decimal,
    received_at: datetime,
    source_name: str | None = None,
    reference: str | None = None,
    note: str | None = None,
    reconcile_existing_orders: bool = True,
) -> tuple[InventoryBatch, int]:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    cost = Decimal(unit_cost)
    if cost < 0:
        raise ValueError("unit_cost must not be negative")

    received = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=UTC)
    batch = InventoryBatch(
        product_id=product.id,
        received_at=received,
        quantity_received=quantity,
        quantity_remaining=quantity,
        unit_cost=cost,
        source_name=(source_name or "").strip() or None,
        reference=(reference or "").strip() or None,
        note=(note or "").strip() or None,
    )
    session.add(batch)
    session.flush()

    allocated = 0
    if reconcile_existing_orders:
        allocated = reconcile_product_orders_from_batch(session, batch=batch)
    return batch, allocated
