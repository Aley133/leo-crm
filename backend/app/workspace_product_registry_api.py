from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product, ProductStatus
from .product_registry_api import ProductRegistryRow, ProductRegistryUpdate, _product_rows
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/products", tags=["workspace-products"])


@router.get("", response_model=list[ProductRegistryRow])
def list_workspace_products(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: ProductStatus | None = None,
    only_without_supplier: bool = False,
    only_failures: bool = False,
    only_monitored: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> list[ProductRegistryRow]:
    statement = (
        select(Product)
        .where(Product.workspace_id == principal.workspace_id)
        .order_by(Product.id)
        .offset(offset)
        .limit(limit)
    )
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Product.name.ilike(pattern),
                Product.kaspi_product_id.ilike(pattern),
                Product.merchant_sku.ilike(pattern),
                Product.brand.ilike(pattern),
            )
        )
    if status is not None:
        statement = statement.where(Product.status == status.value)
    rows = _product_rows(db, list(db.scalars(statement).all()))
    if only_without_supplier:
        rows = [row for row in rows if row.supplier_count == 0]
    if only_failures:
        rows = [row for row in rows if row.failed_monitor_count > 0]
    if only_monitored:
        rows = [row for row in rows if row.active_monitor_count > 0]
    return rows


@router.get("/{product_id}", response_model=ProductRegistryRow)
def read_workspace_product(
    product_id: int,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> ProductRegistryRow:
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.workspace_id == principal.workspace_id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_rows(db, [product])[0]


@router.patch("/{product_id}", response_model=ProductRegistryRow)
def update_workspace_product(
    product_id: int,
    payload: ProductRegistryUpdate,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> ProductRegistryRow:
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.workspace_id == principal.workspace_id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    values = payload.model_dump(exclude_unset=True)
    if "status" in values and values["status"] is not None:
        values["status"] = values["status"].value
    for field, value in values.items():
        setattr(product, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(product)
    return _product_rows(db, [product])[0]
