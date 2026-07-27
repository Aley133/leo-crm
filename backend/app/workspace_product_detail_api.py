from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product
from .product_detail_api import ProductDetailResponse, get_product_detail
from .workspace_auth import WorkspaceSession, require_workspace_session


router = APIRouter(prefix="/api/workspace/products", tags=["workspace-product-detail"])


@router.get("/{product_id}/detail", response_model=ProductDetailResponse)
def get_workspace_product_detail(
    product_id: int,
    observation_limit: int = Query(default=100, ge=1, le=500),
    session: WorkspaceSession = Depends(require_workspace_session),
    db: Session = Depends(get_db),
) -> ProductDetailResponse:
    product = db.get(Product, product_id)
    if product is None or product.workspace_id != session.workspace_id:
        raise HTTPException(status_code=404, detail="Product not found")
    return get_product_detail(product_id=product_id, observation_limit=observation_limit, db=db)
