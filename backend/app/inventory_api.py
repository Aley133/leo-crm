from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .inventory_models import InventoryAllocation, InventoryBatch, InventoryBatchType
from .inventory_service import (
    build_incoming_reservations,
    complete_production_order,
    create_inventory_batch,
    mark_inventory_batch_received,
    rebuild_product_fifo,
)
from .models import MarketplaceOrderLine, Product
from .product_inventory_group import (
    inventory_group_products,
    inventory_owner_product,
    inventory_owner_product_id,
)


class InventoryBatchCreate(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)
    unit_cost: Decimal = Field(ge=0)
    received_at: datetime | None = None
    source_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    reconcile_existing_orders: bool = True
    is_received: bool = True
    batch_type: InventoryBatchType = InventoryBatchType.PURCHASE


class InventoryBatchUpdate(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)
    unit_cost: Decimal = Field(ge=0)
    received_at: datetime
    source_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    batch_type: InventoryBatchType | None = None


class InventoryOwnerUpdate(BaseModel):
    owner_product_id: int = Field(gt=0)


class ProductionOrderRead(BaseModel):
    order_id: int
    order_line_id: int
    external_code: str | None
    ordered_at: datetime | None
    order_quantity: int
    reserved_quantity: int


class InventoryBatchRead(BaseModel):
    id: int
    received_at: datetime
    quantity_received: int
    quantity_remaining: int
    quantity_allocated: int
    unit_cost: Decimal
    is_received: bool
    batch_type: InventoryBatchType
    source_name: str | None
    reference: str | None
    note: str | None
    can_delete: bool
    can_edit: bool = True
    can_receive: bool = False
    production_orders: list[ProductionOrderRead] = Field(default_factory=list)


class InventoryGroupProductRead(BaseModel):
    product_id: int
    name: str
    kaspi_product_id: str
    merchant_sku: str | None


class ProductInventoryRead(BaseModel):
    product_id: int
    inventory_owner_product_id: int
    inventory_owner_name: str
    shared_products: list[InventoryGroupProductRead]
    on_hand: int
    received_total: int
    expected_total: int
    allocated_total: int
    production_planned_total: int
    production_completed_total: int
    production_remaining_total: int
    batches: list[InventoryBatchRead]


class InventoryBatchCreated(BaseModel):
    batch: InventoryBatchRead
    allocated_to_existing_orders: int
    on_hand: int


class InventoryBatchUpdated(BaseModel):
    batch: InventoryBatchRead
    reallocated_quantity: int
    on_hand: int


class ProductionCompletionRead(BaseModel):
    batch_id: int
    order_id: int
    order_line_id: int
    external_code: str | None
    completed_quantity: int
    order_line_fully_allocated: bool


router = APIRouter(
    prefix="/api/products",
    tags=["inventory"],
    dependencies=[Depends(require_service_token)],
)


def _batch_read(
    batch: InventoryBatch,
    *,
    allocated: int,
    production_orders: list[ProductionOrderRead] | None = None,
) -> InventoryBatchRead:
    is_production = batch.batch_type == InventoryBatchType.PRODUCTION.value
    displayed_remaining = (
        max(int(batch.quantity_received) - allocated, 0)
        if is_production
        else int(batch.quantity_remaining)
    )
    mutable = not is_production or allocated == 0
    return InventoryBatchRead(
        id=batch.id,
        received_at=batch.received_at,
        quantity_received=batch.quantity_received,
        quantity_remaining=displayed_remaining,
        quantity_allocated=allocated,
        unit_cost=Decimal(batch.unit_cost),
        is_received=bool(batch.is_received),
        batch_type=InventoryBatchType(batch.batch_type),
        source_name=batch.source_name,
        reference=batch.reference,
        note=batch.note,
        can_delete=mutable,
        can_edit=mutable,
        can_receive=not is_production and not batch.is_received,
        production_orders=production_orders or [],
    )


def _product_inventory(db: Session, product_id: int) -> ProductInventoryRead:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    owner = inventory_owner_product(db, product)
    group_products = inventory_group_products(db, owner)

    batches = db.scalars(
        select(InventoryBatch)
        .where(InventoryBatch.product_id == owner.id)
        .order_by(InventoryBatch.received_at.desc(), InventoryBatch.id.desc())
    ).all()
    batch_ids = [batch.id for batch in batches]
    allocated_by_batch = {
        int(batch_id): int(quantity or 0)
        for batch_id, quantity in (
            db.execute(
                select(
                    InventoryAllocation.inventory_batch_id,
                    func.coalesce(func.sum(InventoryAllocation.quantity), 0),
                )
                .where(InventoryAllocation.inventory_batch_id.in_(batch_ids))
                .group_by(InventoryAllocation.inventory_batch_id)
            ).all()
            if batch_ids
            else []
        )
    }
    production_orders_by_batch: dict[int, list[ProductionOrderRead]] = defaultdict(list)
    for reservation in build_incoming_reservations(db, product_ids={int(owner.id)}):
        if reservation.batch_type != InventoryBatchType.PRODUCTION.value:
            continue
        production_orders_by_batch[reservation.batch_id].append(
            ProductionOrderRead(
                order_id=reservation.order_id,
                order_line_id=reservation.order_line_id,
                external_code=reservation.external_code,
                ordered_at=reservation.ordered_at,
                order_quantity=reservation.line_quantity,
                reserved_quantity=reservation.reserved_quantity,
            )
        )

    received_batches = [
        batch
        for batch in batches
        if (
            batch.batch_type == InventoryBatchType.PURCHASE.value
            and batch.is_received
        )
    ]
    expected_purchase_batches = [
        batch
        for batch in batches
        if (
            batch.batch_type == InventoryBatchType.PURCHASE.value
            and not batch.is_received
        )
    ]
    production_batches = [
        batch
        for batch in batches
        if batch.batch_type == InventoryBatchType.PRODUCTION.value
    ]
    received_total = sum(int(batch.quantity_received) for batch in received_batches)
    production_planned_total = sum(
        int(batch.quantity_received) for batch in production_batches
    )
    production_completed_total = sum(
        allocated_by_batch.get(batch.id, 0) for batch in production_batches
    )
    production_remaining_total = max(
        production_planned_total - production_completed_total,
        0,
    )
    expected_total = (
        sum(int(batch.quantity_received) for batch in expected_purchase_batches)
        + production_remaining_total
    )
    on_hand = sum(int(batch.quantity_remaining) for batch in received_batches)
    return ProductInventoryRead(
        product_id=product_id,
        inventory_owner_product_id=int(owner.id),
        inventory_owner_name=owner.name,
        shared_products=[
            InventoryGroupProductRead(
                product_id=int(member.id),
                name=member.name,
                kaspi_product_id=member.kaspi_product_id,
                merchant_sku=member.merchant_sku,
            )
            for member in group_products
        ],
        on_hand=on_hand,
        received_total=received_total,
        expected_total=expected_total,
        allocated_total=sum(allocated_by_batch.values()),
        production_planned_total=production_planned_total,
        production_completed_total=production_completed_total,
        production_remaining_total=production_remaining_total,
        batches=[
            _batch_read(
                batch,
                allocated=allocated_by_batch.get(batch.id, 0),
                production_orders=production_orders_by_batch.get(batch.id),
            )
            for batch in batches
        ],
    )


@router.get("/{product_id}/inventory", response_model=ProductInventoryRead)
def get_product_inventory(
    product_id: int,
    db: Session = Depends(get_db),
) -> ProductInventoryRead:
    return _product_inventory(db, product_id)


@router.put("/{product_id}/inventory-owner", response_model=ProductInventoryRead)
def merge_product_inventory(
    product_id: int,
    payload: InventoryOwnerUpdate,
    db: Session = Depends(get_db),
) -> ProductInventoryRead:
    source_product = db.scalar(
        select(Product).where(Product.id == product_id).with_for_update()
    )
    target_product = db.scalar(
        select(Product)
        .where(Product.id == payload.owner_product_id)
        .with_for_update()
    )
    if source_product is None or target_product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if int(source_product.workspace_id) != int(target_product.workspace_id):
        raise HTTPException(
            status_code=409,
            detail="Нельзя объединить товары разных аккаунтов",
        )

    source_owner = inventory_owner_product(db, source_product)
    target_owner = inventory_owner_product(db, target_product)
    if int(source_owner.id) == int(target_owner.id):
        return _product_inventory(db, product_id)

    source_members = inventory_group_products(db, source_owner)
    db.scalars(
        select(InventoryBatch)
        .where(
            InventoryBatch.product_id.in_((source_owner.id, target_owner.id))
        )
        .with_for_update()
    ).all()
    for batch in db.scalars(
        select(InventoryBatch).where(InventoryBatch.product_id == source_owner.id)
    ).all():
        batch.product_id = int(target_owner.id)
    for member in source_members:
        member.inventory_owner_product_id = int(target_owner.id)

    db.flush()
    rebuild_product_fifo(db, product_id=int(target_owner.id))
    db.commit()
    return _product_inventory(db, product_id)


@router.post("/{product_id}/inventory/batches", response_model=InventoryBatchCreated)
def add_product_inventory_batch(
    product_id: int,
    payload: InventoryBatchCreate,
    db: Session = Depends(get_db),
) -> InventoryBatchCreated:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    try:
        batch, allocated = create_inventory_batch(
            db,
            product=product,
            quantity=payload.quantity,
            unit_cost=payload.unit_cost,
            received_at=payload.received_at or datetime.now(UTC),
            source_name=payload.source_name,
            reference=payload.reference,
            note=payload.note,
            reconcile_existing_orders=payload.reconcile_existing_orders,
            is_received=payload.is_received,
            batch_type=payload.batch_type,
        )
        db.commit()
        db.refresh(batch)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    snapshot = _product_inventory(db, product_id)
    return InventoryBatchCreated(
        batch=next(row for row in snapshot.batches if row.id == batch.id),
        allocated_to_existing_orders=allocated,
        on_hand=snapshot.on_hand,
    )


@router.post(
    "/{product_id}/inventory/batches/{batch_id}/receive",
    response_model=InventoryBatchUpdated,
)
def receive_product_inventory_batch(
    product_id: int,
    batch_id: int,
    db: Session = Depends(get_db),
) -> InventoryBatchUpdated:
    try:
        owner_id = inventory_owner_product_id(db, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == owner_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    try:
        allocated = mark_inventory_batch_received(
            db,
            batch=batch,
            received_at=datetime.now(UTC),
        )
        db.commit()
        db.refresh(batch)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = _product_inventory(db, product_id)
    return InventoryBatchUpdated(
        batch=next(row for row in snapshot.batches if row.id == batch.id),
        reallocated_quantity=allocated,
        on_hand=snapshot.on_hand,
    )


@router.post(
    "/{product_id}/inventory/batches/{batch_id}/orders/{order_line_id}/manufacture",
    response_model=ProductionCompletionRead,
)
def manufacture_product_order(
    product_id: int,
    batch_id: int,
    order_line_id: int,
    db: Session = Depends(get_db),
) -> ProductionCompletionRead:
    try:
        owner_id = inventory_owner_product_id(db, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == owner_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    order_line = db.scalar(
        select(MarketplaceOrderLine)
        .where(
            MarketplaceOrderLine.id == order_line_id,
        )
        .with_for_update()
    )
    if (
        order_line is None
        or order_line.product_id is None
        or inventory_owner_product_id(db, int(order_line.product_id)) != owner_id
    ):
        raise HTTPException(status_code=404, detail="Order line not found")

    try:
        completed = complete_production_order(
            db,
            batch=batch,
            order_line=order_line,
            completed_at=datetime.now(UTC),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ProductionCompletionRead(
        batch_id=completed.batch_id,
        order_id=completed.order_id,
        order_line_id=completed.order_line_id,
        external_code=completed.external_code,
        completed_quantity=completed.completed_quantity,
        order_line_fully_allocated=completed.order_line_fully_allocated,
    )


@router.patch(
    "/{product_id}/inventory/batches/{batch_id}",
    response_model=InventoryBatchUpdated,
)
def update_product_inventory_batch(
    product_id: int,
    batch_id: int,
    payload: InventoryBatchUpdate,
    db: Session = Depends(get_db),
) -> InventoryBatchUpdated:
    try:
        owner_id = inventory_owner_product_id(db, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == owner_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    allocated = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryAllocation.quantity), 0)).where(
                InventoryAllocation.inventory_batch_id == batch.id
            )
        )
        or 0
    )
    if (
        batch.batch_type == InventoryBatchType.PRODUCTION.value
        and allocated > 0
    ):
        raise HTTPException(
            status_code=409,
            detail="Manufactured production batch cannot be edited",
        )
    if (
        payload.batch_type is not None
        and payload.batch_type.value != batch.batch_type
        and allocated > 0
    ):
        raise HTTPException(
            status_code=409,
            detail="Allocated inventory batch type cannot be changed",
        )

    received = payload.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)

    target_batch_type = payload.batch_type or InventoryBatchType(batch.batch_type)
    batch.quantity_received = payload.quantity
    batch.batch_type = target_batch_type.value
    if target_batch_type is InventoryBatchType.PRODUCTION:
        batch.is_received = False
        batch.quantity_remaining = 0
    else:
        batch.quantity_remaining = payload.quantity if batch.is_received else 0
    batch.unit_cost = payload.unit_cost
    batch.received_at = received
    batch.source_name = (payload.source_name or "").strip() or None
    batch.reference = (payload.reference or "").strip() or None
    batch.note = (payload.note or "").strip() or None

    reallocated = rebuild_product_fifo(db, product_id=owner_id)
    db.commit()
    db.refresh(batch)

    snapshot = _product_inventory(db, product_id)
    return InventoryBatchUpdated(
        batch=next(row for row in snapshot.batches if row.id == batch.id),
        reallocated_quantity=reallocated,
        on_hand=snapshot.on_hand,
    )


@router.delete(
    "/{product_id}/inventory/batches/{batch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_product_inventory_batch(
    product_id: int,
    batch_id: int,
    db: Session = Depends(get_db),
) -> Response:
    try:
        owner_id = inventory_owner_product_id(db, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == owner_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    allocated = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryAllocation.quantity), 0)).where(
                InventoryAllocation.inventory_batch_id == batch.id
            )
        )
        or 0
    )
    if (
        batch.batch_type == InventoryBatchType.PRODUCTION.value
        and allocated > 0
    ):
        raise HTTPException(
            status_code=409,
            detail="Manufactured production batch cannot be deleted",
        )

    product_batch_ids = select(InventoryBatch.id).where(
        InventoryBatch.product_id == owner_id,
        InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
    )
    db.execute(
        delete(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id.in_(product_batch_ids)
        )
    )
    db.flush()
    db.delete(batch)
    db.flush()
    rebuild_product_fifo(db, product_id=owner_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
