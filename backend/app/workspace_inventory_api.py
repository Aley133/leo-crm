from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .inventory_api import (
    InventoryBatchCreate,
    InventoryBatchCreated,
    InventoryBatchRead,
    InventoryBatchUpdate,
    InventoryBatchUpdated,
    ProductInventoryRead,
    _batch_read,
    _on_hand,
)
from .inventory_models import InventoryAllocation, InventoryBatch
from .inventory_service import create_inventory_batch, rebuild_product_fifo
from .models import Product
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/inventory", tags=["workspace-inventory"])


def _workspace_product(db: Session, *, product_id: int, workspace_id: int) -> Product:
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.workspace_id == workspace_id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("/{product_id}", response_model=ProductInventoryRead)
def get_workspace_inventory(
    product_id: int,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> ProductInventoryRead:
    _workspace_product(db, product_id=product_id, workspace_id=principal.workspace_id)
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


@router.post("/{product_id}/batches", response_model=InventoryBatchCreated)
def create_workspace_inventory_batch(
    product_id: int,
    payload: InventoryBatchCreate,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> InventoryBatchCreated:
    product = _workspace_product(db, product_id=product_id, workspace_id=principal.workspace_id)
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


@router.patch("/{product_id}/batches/{batch_id}", response_model=InventoryBatchUpdated)
def update_workspace_inventory_batch(
    product_id: int,
    batch_id: int,
    payload: InventoryBatchUpdate,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> InventoryBatchUpdated:
    _workspace_product(db, product_id=product_id, workspace_id=principal.workspace_id)
    batch = db.scalar(
        select(InventoryBatch)
        .where(InventoryBatch.id == batch_id, InventoryBatch.product_id == product_id)
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


@router.delete("/{product_id}/batches/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace_inventory_batch(
    product_id: int,
    batch_id: int,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> Response:
    _workspace_product(db, product_id=product_id, workspace_id=principal.workspace_id)
    batch = db.scalar(
        select(InventoryBatch)
        .where(InventoryBatch.id == batch_id, InventoryBatch.product_id == product_id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found")
    product_batch_ids = select(InventoryBatch.id).where(InventoryBatch.product_id == product_id)
    from sqlalchemy import delete
    db.execute(delete(InventoryAllocation).where(InventoryAllocation.inventory_batch_id.in_(product_batch_ids)))
    db.flush()
    db.delete(batch)
    db.flush()
    rebuild_product_fifo(db, product_id=product_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
