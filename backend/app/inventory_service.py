from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .inventory_models import InventoryAllocation, InventoryBatch, InventoryBatchType
from .models import (
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    Product,
)
from .product_inventory_group import (
    inventory_group_product_ids,
    inventory_owner_product,
    inventory_owner_product_id,
)


_TERMINAL_ORDER_STATUSES = {
    MarketplaceOrderStatus.CANCELLING.value,
    MarketplaceOrderStatus.CANCELLED.value,
    MarketplaceOrderStatus.RETURNED.value,
}
_INCOMING_ORDER_STATUSES = {
    MarketplaceOrderStatus.NEW.value,
    MarketplaceOrderStatus.ACCEPTED.value,
    MarketplaceOrderStatus.ASSEMBLY.value,
    "preorder",
}
_MANUAL_INVENTORY_BLOCKING_STAGES = {"cancelled", "returned"}


def _sync_product_inventory_to_feed(
    session: Session,
    *,
    product_id: int,
    reason: str,
) -> None:
    # Lazy import avoids commerce -> inventory -> dumping -> commerce during
    # application startup while keeping the inventory transaction authoritative.
    from .dumping_service import sync_product_inventory_to_feed

    for member_product_id in inventory_group_product_ids(session, product_id):
        sync_product_inventory_to_feed(
            session,
            product_id=member_product_id,
            reason=reason,
        )


def _order_blocks_inventory_allocation(order: MarketplaceOrder) -> bool:
    return (
        order.status in _TERMINAL_ORDER_STATUSES
        or order.manual_stage in _MANUAL_INVENTORY_BLOCKING_STAGES
    )


def _close_untracked_order_offer(
    session: Session,
    *,
    sku_candidates: set[str],
) -> None:
    from .dumping_service import close_untracked_order_offer

    close_untracked_order_offer(session, sku_candidates=sku_candidates)


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


@dataclass(frozen=True, slots=True)
class InventoryReleaseResult:
    released_quantity: int
    affected_batch_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class IncomingReservation:
    batch_id: int
    batch_type: str
    product_id: int
    order_id: int
    order_line_id: int
    external_code: str | None
    ordered_at: datetime | None
    line_quantity: int
    reserved_quantity: int


@dataclass(frozen=True, slots=True)
class ProductionCompletionResult:
    batch_id: int
    order_id: int
    order_line_id: int
    external_code: str | None
    completed_quantity: int
    order_line_fully_allocated: bool


def allocated_quantity_for_line(session: Session, order_line_id: int) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(InventoryAllocation.quantity), 0)).where(
                InventoryAllocation.marketplace_order_line_id == order_line_id
            )
        )
        or 0
    )


def release_cancelled_order_inventory(
    session: Session,
    *,
    order: MarketplaceOrder,
    released_at: datetime | None = None,
    force: bool = False,
    reason: str = "order_cancelled_before_courier_handoff",
) -> InventoryReleaseResult:
    """Return pre-handover cancellation allocations to their original batches.

    Kaspi's final ``CANCELLED`` state means the order did not complete the
    warehouse-to-courier handoff. Post-handoff returns use the separate
    ``RETURNED`` state and deliberately remain allocated until a physical return
    is accepted by the warehouse.

    Deleting the allocation and restoring ``quantity_remaining`` in one caller-
    owned transaction makes the operation idempotent: a repeated observation of
    the same cancelled order finds no allocations and cannot add stock twice.
    """
    if not force and order.status != MarketplaceOrderStatus.CANCELLED.value:
        return InventoryReleaseResult(0, ())

    rows = session.execute(
        select(InventoryAllocation, InventoryBatch)
        .join(
            InventoryBatch,
            InventoryBatch.id == InventoryAllocation.inventory_batch_id,
        )
        .join(
            MarketplaceOrderLine,
            MarketplaceOrderLine.id
            == InventoryAllocation.marketplace_order_line_id,
        )
        .where(
            MarketplaceOrderLine.marketplace_order_id == order.id,
            InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
        )
        .order_by(InventoryBatch.id, InventoryAllocation.id)
        .with_for_update()
    ).all()
    if not rows:
        return InventoryReleaseResult(0, ())

    released_by_batch: dict[int, int] = defaultdict(int)
    audit_allocations: list[dict[str, int | str]] = []
    batches_by_id: dict[int, InventoryBatch] = {}
    for allocation, batch in rows:
        quantity = int(allocation.quantity or 0)
        if quantity <= 0:
            continue
        batch_id = int(batch.id)
        released_by_batch[batch_id] += quantity
        batches_by_id[batch_id] = batch
        audit_allocations.append(
            {
                "allocation_id": int(allocation.id),
                "batch_id": batch_id,
                "order_line_id": int(allocation.marketplace_order_line_id),
                "quantity": quantity,
                "unit_cost": str(Decimal(allocation.unit_cost)),
            }
        )

    released_quantity = sum(released_by_batch.values())
    if released_quantity <= 0:
        return InventoryReleaseResult(0, ())

    for batch_id, quantity in released_by_batch.items():
        batch = batches_by_id[batch_id]
        restored_remaining = int(batch.quantity_remaining) + quantity
        if restored_remaining > int(batch.quantity_received):
            raise ValueError(
                "inventory cancellation release would exceed received batch quantity"
            )
        batch.quantity_remaining = restored_remaining

    for allocation, _batch in rows:
        session.delete(allocation)

    now = released_at or datetime.now(UTC)
    event_key = f"inventory_released:{reason}:v{order.version}"
    existing_event = session.scalar(
        select(MarketplaceOrderEvent).where(
            MarketplaceOrderEvent.marketplace_order_id == order.id,
            MarketplaceOrderEvent.source_event_key == event_key,
        )
    )
    if existing_event is None:
        order.events.append(
            MarketplaceOrderEvent(
                source_event_key=event_key,
                event_type="inventory_released",
                previous_status=MarketplaceOrderStatus.CANCELLED.value,
                current_status=MarketplaceOrderStatus.CANCELLED.value,
                occurred_at=now,
                metadata_json={
                    "reason": reason,
                    "released_quantity": released_quantity,
                    "allocations": audit_allocations,
                },
            )
        )

    session.flush()
    for product_id in sorted({int(batch.product_id) for _allocation, batch in rows}):
        _sync_product_inventory_to_feed(
            session,
            product_id=product_id,
            reason="cancelled_order_inventory_released",
        )
    return InventoryReleaseResult(
        released_quantity=released_quantity,
        affected_batch_ids=tuple(sorted(released_by_batch)),
    )


def build_incoming_reservations(
    session: Session,
    *,
    product_ids: set[int] | None = None,
) -> tuple[IncomingReservation, ...]:
    """Reserve expected purchases and production capacity by batch and order FIFO."""
    if product_ids is not None and not product_ids:
        return ()

    requested_owner_ids = (
        {
            inventory_owner_product_id(session, product_id)
            for product_id in product_ids
        }
        if product_ids is not None
        else None
    )
    batch_query = (
        select(InventoryBatch)
        .where(InventoryBatch.is_received.is_(False))
        .order_by(
            InventoryBatch.product_id,
            InventoryBatch.received_at,
            InventoryBatch.id,
        )
    )
    if requested_owner_ids is not None:
        batch_query = batch_query.where(
            InventoryBatch.product_id.in_(requested_owner_ids)
        )
    batches = session.scalars(batch_query).all()
    if not batches:
        return ()

    batch_ids = [batch.id for batch in batches]
    allocated_by_batch = {
        int(batch_id): int(quantity or 0)
        for batch_id, quantity in session.execute(
            select(
                InventoryAllocation.inventory_batch_id,
                func.coalesce(func.sum(InventoryAllocation.quantity), 0),
            )
            .where(InventoryAllocation.inventory_batch_id.in_(batch_ids))
            .group_by(InventoryAllocation.inventory_batch_id)
        ).all()
    }

    expected_product_ids = {int(batch.product_id) for batch in batches}
    candidate_rows = session.execute(
        select(MarketplaceOrderLine, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .join(Product, Product.id == MarketplaceOrderLine.product_id)
        .where(
            or_(
                Product.id.in_(expected_product_ids),
                Product.inventory_owner_product_id.in_(expected_product_ids),
            ),
            MarketplaceOrder.status.in_(_INCOMING_ORDER_STATUSES),
            or_(
                MarketplaceOrder.manual_stage.is_(None),
                MarketplaceOrder.manual_stage.not_in(
                    _MANUAL_INVENTORY_BLOCKING_STAGES
                ),
            ),
        )
        .order_by(
            MarketplaceOrder.ordered_at.asc().nullsfirst(),
            MarketplaceOrder.id,
            MarketplaceOrderLine.id,
        )
    ).all()
    if not candidate_rows:
        return ()

    line_ids = [line.id for line, _order in candidate_rows]
    allocated_by_line = {
        int(line_id): int(quantity or 0)
        for line_id, quantity in session.execute(
            select(
                InventoryAllocation.marketplace_order_line_id,
                func.coalesce(func.sum(InventoryAllocation.quantity), 0),
            )
            .where(InventoryAllocation.marketplace_order_line_id.in_(line_ids))
            .group_by(InventoryAllocation.marketplace_order_line_id)
        ).all()
    }

    candidates_by_product: dict[int, list[tuple[MarketplaceOrderLine, MarketplaceOrder]]] = (
        defaultdict(list)
    )
    remaining_by_line: dict[int, int] = {}
    for line, order in candidate_rows:
        if line.product_id is None:
            continue
        owner_id = inventory_owner_product_id(session, int(line.product_id))
        candidates_by_product[owner_id].append((line, order))
        remaining_by_line[line.id] = max(
            int(line.quantity or 0) - allocated_by_line.get(line.id, 0),
            0,
        )

    reservations: list[IncomingReservation] = []
    for batch in batches:
        capacity = int(batch.quantity_received)
        if batch.batch_type == InventoryBatchType.PRODUCTION.value:
            capacity -= allocated_by_batch.get(batch.id, 0)
        capacity = max(capacity, 0)
        if capacity <= 0:
            continue

        for line, order in candidates_by_product.get(int(batch.product_id), ()):
            needed = remaining_by_line.get(line.id, 0)
            if needed <= 0:
                continue
            quantity = min(needed, capacity)
            if quantity <= 0:
                continue
            reservations.append(
                IncomingReservation(
                    batch_id=batch.id,
                    batch_type=batch.batch_type,
                    product_id=int(batch.product_id),
                    order_id=order.id,
                    order_line_id=line.id,
                    external_code=order.external_code,
                    ordered_at=order.ordered_at,
                    line_quantity=int(line.quantity or 0),
                    reserved_quantity=quantity,
                )
            )
            remaining_by_line[line.id] = needed - quantity
            capacity -= quantity
            if capacity <= 0:
                break
    return tuple(reservations)


def allocate_order_line_fifo(
    session: Session,
    *,
    order_line: MarketplaceOrderLine,
    order: MarketplaceOrder | None = None,
    allocated_at: datetime | None = None,
    sync_feed: bool = True,
) -> AllocationResult:
    requested = max(int(order_line.quantity or 0), 0)
    previous = allocated_quantity_for_line(session, order_line.id)
    needed = max(requested - previous, 0)
    resolved_order = order or session.get(MarketplaceOrder, order_line.marketplace_order_id)
    if resolved_order is None or _order_blocks_inventory_allocation(resolved_order):
        return AllocationResult(requested, previous, 0)
    if order_line.product_id is None:
        _close_untracked_order_offer(
            session,
            sku_candidates={
                order_line.merchant_sku or "",
                order_line.external_product_id or "",
            },
        )
        return AllocationResult(requested, previous, 0)
    if needed == 0:
        return AllocationResult(requested, previous, 0)

    now = allocated_at or datetime.now(UTC)

    owner_product_id = inventory_owner_product_id(session, int(order_line.product_id))
    batches = session.scalars(
        select(InventoryBatch)
        .where(
            InventoryBatch.product_id == owner_product_id,
            InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
            InventoryBatch.is_received.is_(True),
            InventoryBatch.quantity_remaining > 0,
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
    if sync_feed:
        _sync_product_inventory_to_feed(
            session,
            product_id=int(order_line.product_id),
            reason="order_inventory_allocated",
        )
    return AllocationResult(requested, previous, newly_allocated)


def reconcile_product_orders_from_batch(
    session: Session,
    *,
    batch: InventoryBatch,
    allocated_at: datetime | None = None,
) -> int:
    if not batch.is_received:
        return 0

    rows = session.execute(
        select(MarketplaceOrderLine, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .join(Product, Product.id == MarketplaceOrderLine.product_id)
        .where(
            or_(
                Product.id == batch.product_id,
                Product.inventory_owner_product_id == batch.product_id,
            ),
            MarketplaceOrder.status.not_in(_TERMINAL_ORDER_STATUSES),
            or_(
                MarketplaceOrder.manual_stage.is_(None),
                MarketplaceOrder.manual_stage.not_in(
                    _MANUAL_INVENTORY_BLOCKING_STAGES
                ),
            ),
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
            sync_feed=False,
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
    owner_product_id = inventory_owner_product_id(session, product_id)
    batch_ids = select(InventoryBatch.id).where(
        InventoryBatch.product_id == owner_product_id,
        InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
    )
    session.execute(
        delete(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id.in_(batch_ids)
        )
    )

    batches = session.scalars(
        select(InventoryBatch)
        .where(InventoryBatch.product_id == owner_product_id)
        .order_by(InventoryBatch.received_at, InventoryBatch.id)
        .with_for_update()
    ).all()
    for batch in batches:
        batch.quantity_remaining = (
            batch.quantity_received
            if (
                batch.batch_type == InventoryBatchType.PURCHASE.value
                and batch.is_received
            )
            else 0
        )
    session.flush()

    rows = session.execute(
        select(MarketplaceOrderLine, MarketplaceOrder)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .join(Product, Product.id == MarketplaceOrderLine.product_id)
        .where(
            or_(
                Product.id == owner_product_id,
                Product.inventory_owner_product_id == owner_product_id,
            ),
            MarketplaceOrder.status.not_in(_TERMINAL_ORDER_STATUSES),
            or_(
                MarketplaceOrder.manual_stage.is_(None),
                MarketplaceOrder.manual_stage.not_in(
                    _MANUAL_INVENTORY_BLOCKING_STAGES
                ),
            ),
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
            sync_feed=False,
        )
        allocated += result.newly_allocated_quantity
    session.flush()
    _sync_product_inventory_to_feed(
        session,
        product_id=owner_product_id,
        reason="fifo_rebuilt",
    )
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
    is_received: bool = True,
    batch_type: InventoryBatchType | str = InventoryBatchType.PURCHASE,
) -> tuple[InventoryBatch, int]:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    cost = Decimal(unit_cost)
    if cost < 0:
        raise ValueError("unit_cost must not be negative")
    try:
        resolved_batch_type = InventoryBatchType(batch_type)
    except ValueError as exc:
        raise ValueError("unknown inventory batch type") from exc
    if resolved_batch_type is InventoryBatchType.PRODUCTION and is_received:
        raise ValueError(
            "production batch cannot be received as stock; complete its orders individually"
        )

    received = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=UTC)
    owner = inventory_owner_product(session, product)
    batch = InventoryBatch(
        product_id=owner.id,
        received_at=received,
        quantity_received=quantity,
        quantity_remaining=(
            quantity
            if is_received and resolved_batch_type is InventoryBatchType.PURCHASE
            else 0
        ),
        unit_cost=cost,
        batch_type=resolved_batch_type.value,
        is_received=is_received,
        source_name=(source_name or "").strip() or None,
        reference=(reference or "").strip() or None,
        note=(note or "").strip() or None,
    )
    session.add(batch)
    session.flush()

    allocated = 0
    if (
        resolved_batch_type is InventoryBatchType.PURCHASE
        and is_received
        and reconcile_existing_orders
    ):
        allocated = reconcile_product_orders_from_batch(session, batch=batch)
    _sync_product_inventory_to_feed(
        session,
        product_id=owner.id,
        reason="inventory_batch_created",
    )
    return batch, allocated


def mark_inventory_batch_received(
    session: Session,
    *,
    batch: InventoryBatch,
    received_at: datetime | None = None,
) -> int:
    if batch.batch_type == InventoryBatchType.PRODUCTION.value:
        raise ValueError(
            "production batch is completed per order with the Manufactured action"
        )
    if batch.is_received:
        return 0
    batch.is_received = True
    batch.received_at = received_at or datetime.now(UTC)
    batch.quantity_remaining = batch.quantity_received
    session.flush()
    allocated = reconcile_product_orders_from_batch(session, batch=batch)
    _sync_product_inventory_to_feed(
        session,
        product_id=int(batch.product_id),
        reason="inventory_batch_received",
    )
    return allocated


def complete_production_order(
    session: Session,
    *,
    batch: InventoryBatch,
    order_line: MarketplaceOrderLine,
    completed_at: datetime | None = None,
) -> ProductionCompletionResult:
    if batch.batch_type != InventoryBatchType.PRODUCTION.value:
        raise ValueError("inventory batch is not a production batch")
    if batch.is_received:
        raise ValueError("production batch must not be received as warehouse stock")
    if order_line.product_id is None or inventory_owner_product_id(
        session,
        int(order_line.product_id),
    ) != int(batch.product_id):
        raise ValueError("order line belongs to another product")

    order = session.get(MarketplaceOrder, order_line.marketplace_order_id)
    if order is None:
        raise ValueError("order not found")
    if order.status not in _INCOMING_ORDER_STATUSES:
        raise ValueError("order is no longer active in the production queue")

    existing = session.scalar(
        select(InventoryAllocation)
        .where(
            InventoryAllocation.inventory_batch_id == batch.id,
            InventoryAllocation.marketplace_order_line_id == order_line.id,
        )
        .with_for_update()
    )
    allocated_before = allocated_quantity_for_line(session, order_line.id)
    if allocated_before >= int(order_line.quantity or 0):
        return ProductionCompletionResult(
            batch_id=batch.id,
            order_id=order.id,
            order_line_id=order_line.id,
            external_code=order.external_code,
            completed_quantity=0,
            order_line_fully_allocated=True,
        )

    reservation = next(
        (
            row
            for row in build_incoming_reservations(
                session,
                product_ids={int(batch.product_id)},
            )
            if row.batch_id == batch.id and row.order_line_id == order_line.id
        ),
        None,
    )
    if reservation is None:
        raise ValueError("order is not reserved by this production batch")

    completed_quantity = int(reservation.reserved_quantity)
    if completed_quantity <= 0:
        raise ValueError("production reservation is empty")

    now = completed_at or datetime.now(UTC)
    if existing is None:
        session.add(
            InventoryAllocation(
                inventory_batch_id=batch.id,
                marketplace_order_line_id=order_line.id,
                quantity=completed_quantity,
                unit_cost=Decimal(batch.unit_cost),
                allocated_at=now,
            )
        )
    else:
        existing.quantity += completed_quantity
        existing.allocated_at = now
    session.flush()

    allocated_after = allocated_quantity_for_line(session, order_line.id)
    return ProductionCompletionResult(
        batch_id=batch.id,
        order_id=order.id,
        order_line_id=order_line.id,
        external_code=order.external_code,
        completed_quantity=completed_quantity,
        order_line_fully_allocated=allocated_after >= int(order_line.quantity or 0),
    )
