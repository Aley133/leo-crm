from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import MarketplaceOrder, MarketplaceOrderLine, Product
from .monitoring import MonitorTarget, SupplierOfferObservation, SupplierOfferState
from .supplier_intelligence import BestOfferEngine, SupplierCandidate, SupplierScore
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ProductDetailHeader(BaseModel):
    id: int
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ProductSalesSummary(BaseModel):
    orders_count: int
    units_sold: int
    revenue_kzt: Decimal
    last_ordered_at: datetime | None


class ProductBindingDetail(BaseModel):
    binding_id: int
    binding_status: str
    is_primary: bool
    priority: int
    supplier_code: str
    supplier_name: str
    supplier_product_id: int
    supplier_product_title: str
    supplier_product_url: str
    monitor_target_id: int | None
    monitor_status: str | None
    consecutive_failures: int
    price: Decimal | None
    old_price: Decimal | None
    currency: str | None
    available: bool | None
    stock: int | None
    delivery_days: int | None
    seller: str | None
    observed_at: datetime | None
    last_checked_at: datetime | None


class ProductObservationRead(BaseModel):
    id: int
    supplier_product_id: int
    supplier_code: str
    price: Decimal | None
    old_price: Decimal | None
    currency: str | None
    available: bool | None
    stock: int | None
    delivery_days: int | None
    seller: str | None
    observed_at: datetime
    created_at: datetime


class SupplierScoreRead(BaseModel):
    binding_id: int
    supplier_product_id: int
    supplier_code: str
    supplier_name: str
    price_score: Decimal
    delivery_score: Decimal
    preference_score: Decimal
    freshness_score: Decimal
    total_score: Decimal
    eligible: bool
    reasons: list[str]


class BestOfferDecisionRead(BaseModel):
    confidence: str
    score_gap: Decimal | None
    runner_up: SupplierScoreRead | None
    warnings: list[str]
    eligible_count: int


class ProductDetailResponse(BaseModel):
    product: ProductDetailHeader
    sales: ProductSalesSummary
    bindings: list[ProductBindingDetail]
    observations: list[ProductObservationRead]
    best_offer: SupplierScoreRead | None
    supplier_scores: list[SupplierScoreRead]
    best_offer_decision: BestOfferDecisionRead


router = APIRouter(
    prefix="/api/products",
    tags=["product-detail"],
    dependencies=[Depends(require_service_token)],
)


def _score_read(score: SupplierScore) -> SupplierScoreRead:
    return SupplierScoreRead(
        binding_id=score.binding_id,
        supplier_product_id=score.supplier_product_id,
        supplier_code=score.supplier_code,
        supplier_name=score.supplier_name,
        price_score=score.price_score,
        delivery_score=score.delivery_score,
        preference_score=score.preference_score,
        freshness_score=score.freshness_score,
        total_score=score.total_score,
        eligible=score.eligible,
        reasons=list(score.reasons),
    )


@router.get("/{product_id}/detail", response_model=ProductDetailResponse)
def get_product_detail(
    product_id: int,
    observation_limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> ProductDetailResponse:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    sales_row = db.execute(
        select(
            func.count(MarketplaceOrderLine.id).label("orders_count"),
            func.coalesce(func.sum(MarketplaceOrderLine.quantity), 0).label("units_sold"),
            func.coalesce(func.sum(MarketplaceOrderLine.line_total), 0).label("revenue_kzt"),
            func.max(MarketplaceOrder.ordered_at).label("last_ordered_at"),
        )
        .select_from(MarketplaceOrderLine)
        .join(MarketplaceOrder, MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id)
        .where(MarketplaceOrderLine.product_id == product_id)
    ).one()

    binding_rows = db.execute(
        select(
            ProductBinding,
            SupplierProduct,
            Supplier,
            MonitorTarget,
            SupplierOfferState,
        )
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(ProductBinding.product_id == product_id)
        .order_by(ProductBinding.is_primary.desc(), ProductBinding.priority, ProductBinding.id)
    ).all()

    supplier_product_ids = [row[1].id for row in binding_rows]
    observations: list[ProductObservationRead] = []
    if supplier_product_ids:
        observation_rows = db.execute(
            select(SupplierOfferObservation, Supplier.code)
            .join(SupplierProduct, SupplierProduct.id == SupplierOfferObservation.supplier_product_id)
            .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
            .where(SupplierOfferObservation.supplier_product_id.in_(supplier_product_ids))
            .order_by(SupplierOfferObservation.observed_at.desc(), SupplierOfferObservation.id.desc())
            .limit(observation_limit)
        ).all()
        observations = [
            ProductObservationRead(
                id=observation.id,
                supplier_product_id=observation.supplier_product_id,
                supplier_code=supplier_code,
                price=observation.price,
                old_price=observation.old_price,
                currency=observation.currency,
                available=observation.available,
                stock=observation.stock,
                delivery_days=observation.delivery_days,
                seller=observation.seller,
                observed_at=observation.observed_at,
                created_at=observation.created_at,
            )
            for observation, supplier_code in observation_rows
        ]

    bindings: list[ProductBindingDetail] = []
    candidates: list[SupplierCandidate] = []
    for binding, supplier_product, supplier, monitor_target, state in binding_rows:
        bindings.append(
            ProductBindingDetail(
                binding_id=binding.id,
                binding_status=binding.status,
                is_primary=binding.is_primary,
                priority=binding.priority,
                supplier_code=supplier.code,
                supplier_name=supplier.name,
                supplier_product_id=supplier_product.id,
                supplier_product_title=supplier_product.title,
                supplier_product_url=supplier_product.url,
                monitor_target_id=None if monitor_target is None else monitor_target.id,
                monitor_status=None if monitor_target is None else monitor_target.status,
                consecutive_failures=0 if monitor_target is None else monitor_target.consecutive_failures,
                price=None if state is None else state.price,
                old_price=None if state is None else state.old_price,
                currency=None if state is None else state.currency,
                available=None if state is None else state.available,
                stock=None if state is None else state.stock,
                delivery_days=None if state is None else state.delivery_days,
                seller=None if state is None else state.seller,
                observed_at=None if state is None else state.observed_at,
                last_checked_at=None if state is None else state.last_checked_at,
            )
        )
        candidates.append(
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
        )

    decision = BestOfferEngine.decide(candidates)
    score_rows = [_score_read(score) for score in decision.ranked]

    return ProductDetailResponse(
        product=ProductDetailHeader(
            id=product.id,
            kaspi_product_id=product.kaspi_product_id,
            merchant_sku=product.merchant_sku,
            name=product.name,
            brand=product.brand,
            status=product.status,
            created_at=product.created_at,
            updated_at=product.updated_at,
        ),
        sales=ProductSalesSummary(
            orders_count=int(sales_row.orders_count or 0),
            units_sold=int(sales_row.units_sold or 0),
            revenue_kzt=Decimal(sales_row.revenue_kzt or 0),
            last_ordered_at=sales_row.last_ordered_at,
        ),
        bindings=bindings,
        observations=observations,
        best_offer=None if decision.best is None else _score_read(decision.best),
        supplier_scores=score_rows,
        best_offer_decision=BestOfferDecisionRead(
            confidence=decision.confidence,
            score_gap=decision.score_gap,
            runner_up=None if decision.runner_up is None else _score_read(decision.runner_up),
            warnings=list(decision.warnings),
            eligible_count=sum(1 for score in decision.ranked if score.eligible),
        ),
    )
