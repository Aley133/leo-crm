from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import Product
from .pricing_models import FxRateSnapshot, PriceCalculation, PricingPolicy
from .pricing_service import calculate_product_price


class PricingPolicyUpsert(BaseModel):
    enabled: bool = True
    target_margin_pct: Decimal = Field(default=30, ge=0, lt=100)
    marketplace_fee_pct: Decimal = Field(default=12, ge=0, lt=100)
    payment_fee_pct: Decimal = Field(default=3, ge=0, lt=100)
    delivery_cost_kzt: Decimal = Field(default=0, ge=0)
    fixed_cost_kzt: Decimal = Field(default=0, ge=0)
    minimum_price_kzt: Decimal | None = Field(default=None, ge=0)
    rounding_step_kzt: int = Field(default=100, ge=1, le=100_000)


class FxSnapshotCreate(BaseModel):
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(default="KZT", min_length=3, max_length=3)
    rate: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=128)
    observed_at: datetime


router = APIRouter(
    prefix="/api/pricing",
    tags=["pricing"],
    dependencies=[Depends(require_service_token)],
)


@router.put("/products/{product_id}/policy")
def upsert_policy(product_id: int, payload: PricingPolicyUpsert, db: Session = Depends(get_db)):
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    policy = db.scalar(select(PricingPolicy).where(PricingPolicy.product_id == product_id))
    values = payload.model_dump()
    if policy is None:
        policy = PricingPolicy(product_id=product_id, **values)
        db.add(policy)
    else:
        for key, value in values.items():
            setattr(policy, key, value)
    db.commit()
    db.refresh(policy)
    return policy


@router.post("/fx", status_code=status.HTTP_201_CREATED)
def create_fx_snapshot(payload: FxSnapshotCreate, db: Session = Depends(get_db)):
    base = payload.base_currency.strip().upper()
    quote = payload.quote_currency.strip().upper()
    if base == quote:
        raise HTTPException(status_code=422, detail="FX base and quote currencies must differ")
    snapshot = FxRateSnapshot(
        base_currency=base,
        quote_currency=quote,
        rate=payload.rate,
        source=payload.source.strip(),
        observed_at=payload.observed_at,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.post("/products/{product_id}/calculate", status_code=status.HTTP_201_CREATED)
def calculate_price(product_id: int, db: Session = Depends(get_db)):
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    calculation = calculate_product_price(db, product_id=product_id)
    db.commit()
    db.refresh(calculation)
    return calculation


@router.get("/products/{product_id}/latest")
def latest_calculation(product_id: int, db: Session = Depends(get_db)):
    calculation = db.scalar(
        select(PriceCalculation)
        .where(PriceCalculation.product_id == product_id)
        .order_by(PriceCalculation.id.desc())
        .limit(1)
    )
    if calculation is None:
        raise HTTPException(status_code=404, detail="Price calculation not found")
    return calculation
