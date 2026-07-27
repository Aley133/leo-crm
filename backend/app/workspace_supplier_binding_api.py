from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product
from .product_supplier_binding_api import (
    ManualSupplierBindingCreate,
    ManualSupplierBindingResult,
    create_manual_supplier_binding,
)
from .workspace_auth import WorkspacePrincipal, require_workspace_principal


router = APIRouter(
    prefix="/api/workspace/products",
    tags=["workspace-supplier-bindings"],
)


@router.post(
    "/{product_id}/supplier-bindings/manual",
    response_model=ManualSupplierBindingResult,
    status_code=status.HTTP_201_CREATED,
)
def create_workspace_manual_supplier_binding(
    product_id: int,
    payload: ManualSupplierBindingCreate,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> ManualSupplierBindingResult:
    """Create a supplier binding only for a product owned by the current workspace.

    Supplier and supplier-product records may be shared technical reference data,
    but the binding is always anchored to an owned Product. Returning 404 for a
    foreign product prevents both cross-workspace discovery and mutation.
    """

    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.workspace_id == principal.workspace_id,
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    return create_manual_supplier_binding(product_id=product.id, payload=payload, db=db)
