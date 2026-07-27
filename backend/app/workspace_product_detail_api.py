from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .action_api import ActionRecommendationRead, get_product_action_recommendation
from .db import get_db
from .models import Product
from .product_detail_api import ProductDetailResponse, get_product_detail
from .product_economics_api import ProductEconomicsRead, get_product_economics
from .workspace_auth import WorkspaceSession, require_workspace_session


router = APIRouter(prefix="/api/workspace/products", tags=["workspace-product-detail"])


def _owned_product(
    db: Session,
    *,
    product_id: int,
    workspace_id: int,
) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("/{product_id}/detail", response_model=ProductDetailResponse)
def get_workspace_product_detail(
    product_id: int,
    observation_limit: int = Query(default=100, ge=1, le=500),
    session: WorkspaceSession = Depends(require_workspace_session),
    db: Session = Depends(get_db),
) -> ProductDetailResponse:
    _owned_product(db, product_id=product_id, workspace_id=session.workspace_id)
    return get_product_detail(product_id=product_id, observation_limit=observation_limit, db=db)


@router.get("/{product_id}/economics", response_model=ProductEconomicsRead)
def get_workspace_product_economics(
    product_id: int,
    session: WorkspaceSession = Depends(require_workspace_session),
    db: Session = Depends(get_db),
) -> ProductEconomicsRead:
    _owned_product(db, product_id=product_id, workspace_id=session.workspace_id)
    return get_product_economics(product_id=product_id, db=db)


@router.get("/{product_id}/action", response_model=ActionRecommendationRead)
def get_workspace_product_action(
    product_id: int,
    session: WorkspaceSession = Depends(require_workspace_session),
    db: Session = Depends(get_db),
) -> ActionRecommendationRead:
    _owned_product(db, product_id=product_id, workspace_id=session.workspace_id)
    return get_product_action_recommendation(product_id=product_id, db=db)
