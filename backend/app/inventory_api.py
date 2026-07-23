from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .inventory_models import InventoryAllocation, InventoryBatch
from .inventory_service import create_inventory_batch, rebuild_product_fifo
from .models import Product


class InventoryBatchCreate(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)
    unit_cost: Decimal = Field(ge=0)
    received_at: datetime | None = None
    source_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    reconcile_existing_orders: bool = True


class InventoryBatchUpdate(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)
    unit_cost: Decimal = Field(ge=0)
    received_at: datetime
    source_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=2000)


class InventoryBatchRead(BaseModel):
    id: int
    received_at: datetime
    quantity_received: int
    quantity_remaining: int
    quantity_allocated: int
    unit_cost: Decimal
    source_name: str | None
    reference: str | None
    note: str | None
    can_delete: bool
    can_edit: bool = True


class ProductInventoryRead(BaseModel):
    product_id: int
    on_hand: int
    received_total: int
    allocated_total: int
    batches: list[InventoryBatchRead]


class InventoryBatchCreated(BaseModel):
    batch: InventoryBatchRead
    allocated_to_existing_orders: int
    on_hand: int


class InventoryBatchUpdated(BaseModel):
    batch: InventoryBatchRead
    reallocated_quantity: int
    on_hand: int


router = APIRouter(
    prefix="/api/products",
    tags=["inventory"],
    dependencies=[Depends(require_service_token)],
)


def _batch_read(batch: InventoryBatch) -> InventoryBatchRead:
    allocated = int(batch.quantity_received) - int(batch.quantity_remaining)
    return InventoryBatchRead(
        id=batch.id,
        received_at=batch.received_at,
        quantity_received=batch.quantity_received,
        quantity_remaining=batch.quantity_remaining,
        quantity_allocated=allocated,
        unit_cost=Decimal(batch.unit_cost),
        source_name=batch.source_name,
        reference=batch.reference,
        note=batch.note,
        can_delete=True,
        can_edit=True,
    )


def _on_hand(db: Session, product_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryBatch.quantity_remaining), 0)).where(
                InventoryBatch.product_id == product_id
            )
        )
        or 0
    )


@router.get("/{product_id}/inventory", response_model=ProductInventoryRead)
def get_product_inventory(
    product_id: int,
    db: Session = Depends(get_db),
) -> ProductInventoryRead:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")

    batches = db.scalars(
        select(InventoryBatch)
        .where(InventoryBatch.product_id == product_id)
        .order_by(InventoryBatch.received_at.desc(), InventoryBatch.id.desc())
    ).all()
    received_total = sum(int(batch.quantity_received) for batch in batches)
    on_hand = sum(int(batch.quantity_remaining) for batch in batches)
    return ProductInventoryRead(
        product_id=product_id,
        on_hand=on_hand,
        received_total=received_total,
        allocated_total=received_total - on_hand,
        batches=[_batch_read(batch) for batch in batches],
    )


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
        )
        db.commit()
        db.refresh(batch)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return InventoryBatchCreated(
        batch=_batch_read(batch),
        allocated_to_existing_orders=allocated,
        on_hand=_on_hand(db, product_id),
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
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == product_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    received = payload.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)

    batch.quantity_received = payload.quantity
    batch.quantity_remaining = payload.quantity
    batch.unit_cost = payload.unit_cost
    batch.received_at = received
    batch.source_name = (payload.source_name or "").strip() or None
    batch.reference = (payload.reference or "").strip() or None
    batch.note = (payload.note or "").strip() or None

    reallocated = rebuild_product_fifo(db, product_id=product_id)
    db.commit()
    db.refresh(batch)

    return InventoryBatchUpdated(
        batch=_batch_read(batch),
        reallocated_quantity=reallocated,
        on_hand=_on_hand(db, product_id),
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
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.id == batch_id,
            InventoryBatch.product_id == product_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")

    product_batch_ids = select(InventoryBatch.id).where(
        InventoryBatch.product_id == product_id
    )
    db.execute(
        delete(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id.in_(product_batch_ids)
        )
    )
    db.flush()
    db.delete(batch)
    db.flush()
    rebuild_product_fifo(db, product_id=product_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
