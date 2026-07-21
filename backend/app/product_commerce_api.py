from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import MarketplaceOrder, MarketplaceOrderLine, Product
from .monitoring import SupplierOfferState
from .product_commerce import ProductCommerceAnalyzer, ProductOrderLineFact
from .supplier_intelligence import BestOfferEngine, SupplierCandidate
from .suppliers import ProductBinding, Supplier, SupplierProduct


class CommerceWindowRead(BaseModel):
    days: int
    orders_count: int
    units_ordered: int
    units_delivered: int
    active_units: int
    cancelled_units: int
    delivered_revenue: Decimal
    average_sale_price: Decimal | None
    estimated_procurement_cost: Decimal | None
    estimated_gross_profit_before_fees: Decimal | None
    estimated_gross_margin_pct_before_fees: Decimal | None


class PurchaseRecommendationRead(BaseModel):
    mode: str
    title: str
    reason: str
    target_stock_units: int
    daily_velocity_30d: Decimal
    coverage_days: int
    confidence: str


class ProductCommerceResponse(BaseModel):
    product_id: int
    current_unit_cost: Decimal | None
    cost_source: str | None
    profit_is_estimated: bool
    profit_note: str
    windows: list[CommerceWindowRead]
    purchase_recommendation: PurchaseRecommendationRead


router = APIRouter(
    prefix="/api/products",
    tags=["product-commerce"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/{product_id}/commerce", response_model=ProductCommerceResponse)
def get_product_commerce(
    product_id: int,
    db: Session = Depends(get_db),
) -> ProductCommerceResponse:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")

    cutoff = datetime.now(UTC) - timedelta(days=90)
    order_rows = db.execute(
        select(MarketplaceOrder, MarketplaceOrderLine)
        .join(
            MarketplaceOrderLine,
            MarketplaceOrderLine.marketplace_order_id == MarketplaceOrder.id,
        )
        .where(
            MarketplaceOrderLine.product_id == product_id,
            MarketplaceOrder.ordered_at >= cutoff,
        )
    ).all()
    facts = tuple(
        ProductOrderLineFact(
            order_id=order.id,
            status=order.status,
            quantity=line.quantity,
            line_total=Decimal(line.line_total),
            ordered_at=order.ordered_at,
            delivered_at=order.delivered_at,
        )
        for order, line in order_rows
    )

    supplier_rows = db.execute(
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
        for binding, supplier_product, supplier, state in supplier_rows
    )
    decision = BestOfferEngine.decide(candidates)
    unit_cost = None if decision.best is None else next(
        (
            candidate.price
            for candidate in candidates
            if candidate.binding_id == decision.best.binding_id
        ),
        None,
    )
    cost_source = None if decision.best is None else decision.best.supplier_name

    analysis = ProductCommerceAnalyzer.analyze(
        facts,
        current_unit_cost=unit_cost,
        cost_source=cost_source,
    )
    return ProductCommerceResponse(
        product_id=product_id,
        current_unit_cost=analysis.current_unit_cost,
        cost_source=analysis.cost_source,
        profit_is_estimated=analysis.profit_is_estimated,
        profit_note=(
            "Расчёт до комиссий, налогов, возвратов и фактической FIFO-себестоимости. "
            "После запуска склада он будет заменён бухгалтерски подтверждённой прибылью."
        ),
        windows=[
            CommerceWindowRead(
                days=item.days,
                orders_count=item.orders_count,
                units_ordered=item.units_ordered,
                units_delivered=item.units_delivered,
                active_units=item.active_units,
                cancelled_units=item.cancelled_units,
                delivered_revenue=item.delivered_revenue,
                average_sale_price=item.average_sale_price,
                estimated_procurement_cost=item.estimated_procurement_cost,
                estimated_gross_profit_before_fees=item.estimated_gross_profit_before_fees,
                estimated_gross_margin_pct_before_fees=item.estimated_gross_margin_pct_before_fees,
            )
            for item in analysis.windows
        ],
        purchase_recommendation=PurchaseRecommendationRead(
            mode=analysis.recommendation.mode,
            title=analysis.recommendation.title,
            reason=analysis.recommendation.reason,
            target_stock_units=analysis.recommendation.target_stock_units,
            daily_velocity_30d=analysis.recommendation.daily_velocity_30d,
            coverage_days=analysis.recommendation.coverage_days,
            confidence=analysis.recommendation.confidence,
        ),
    )
