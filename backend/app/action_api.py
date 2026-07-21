from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .action_engine import ActionEngine, ActionRecommendation
from .auth import require_service_token
from .db import get_db
from .models import Product
from .monitoring import SupplierOfferState
from .supplier_intelligence import BestOfferEngine, SupplierCandidate
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ActionRecommendationRead(BaseModel):
    kind: str
    severity: str
    title: str
    summary: str
    reasons: list[str]
    target_binding_id: int | None
    target_supplier_code: str | None
    target_supplier_name: str | None
    score_gap: Decimal | None
    auto_apply_allowed: bool


router = APIRouter(
    prefix="/api/actions",
    tags=["action-engine"],
    dependencies=[Depends(require_service_token)],
)


def _read(recommendation: ActionRecommendation) -> ActionRecommendationRead:
    return ActionRecommendationRead(
        kind=recommendation.kind,
        severity=recommendation.severity,
        title=recommendation.title,
        summary=recommendation.summary,
        reasons=list(recommendation.reasons),
        target_binding_id=recommendation.target_binding_id,
        target_supplier_code=recommendation.target_supplier_code,
        target_supplier_name=recommendation.target_supplier_name,
        score_gap=recommendation.score_gap,
        auto_apply_allowed=recommendation.auto_apply_allowed,
    )


@router.get("/products/{product_id}", response_model=ActionRecommendationRead)
def get_product_action_recommendation(
    product_id: int,
    db: Session = Depends(get_db),
) -> ActionRecommendationRead:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")

    rows = db.execute(
        select(ProductBinding, SupplierProduct, Supplier, SupplierOfferState)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(ProductBinding.product_id == product_id)
        .order_by(ProductBinding.is_primary.desc(), ProductBinding.priority, ProductBinding.id)
    ).all()

    candidates = tuple(
        SupplierCandidate(
            binding_id=binding.id,
            supplier_product_id=supplier_product.id,
            supplier_code=supplier.code,
            supplier_name=supplier.name,
            price=None if state is None else state.price,
            currency=None if state is None else state.currency,
            available=None if state is None else state.available,
            delivery_days=None if state is None else state.delivery_days,
            is_primary=binding.is_primary,
            priority=binding.priority,
            last_checked_at=None if state is None else state.last_checked_at,
        )
        for binding, supplier_product, supplier, state in rows
    )
    decision = BestOfferEngine.decide(candidates)
    return _read(ActionEngine.recommend(candidates, decision))
