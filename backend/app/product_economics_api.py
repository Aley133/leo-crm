from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .commerce.profit_calculator import (
    KASPI_COMMISSION_RATE,
    TAX_RATE,
    calculate_line_economics,
)
from .db import get_db
from .models import MarketplaceOrder, MarketplaceOrderLine, Product
from .monitoring import SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ProductEconomicsRead(BaseModel):
    sale_unit_price: Decimal | None
    procurement_unit_cost: Decimal | None
    procurement_source_name: str | None
    kaspi_commission_rate_pct: Decimal
    tax_rate_pct: Decimal
    kaspi_commission: Decimal | None
    tax: Decimal | None
    logistics: Decimal | None
    net_profit: Decimal | None
    net_margin_pct: Decimal | None
    total_net_profit: Decimal | None
    total_net_margin_pct: Decimal | None
    profit_units_count: int


router = APIRouter(
    prefix="/api/products",
    tags=["product-economics"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/{product_id}/economics", response_model=ProductEconomicsRead)
def get_product_economics(
    product_id: int,
    db: Session = Depends(get_db),
) -> ProductEconomicsRead:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    sale_lines = db.execute(
        select(MarketplaceOrderLine, MarketplaceOrder.status)
        .join(
            MarketplaceOrder,
            MarketplaceOrder.id == MarketplaceOrderLine.marketplace_order_id,
        )
        .where(
            MarketplaceOrderLine.product_id == product_id,
            MarketplaceOrder.status.notin_(("cancelling", "cancelled", "returned")),
        )
        .order_by(
            MarketplaceOrder.ordered_at.desc().nullslast(),
            MarketplaceOrderLine.id.desc(),
        )
    ).all()
    latest_line = sale_lines[0][0] if sale_lines else None
    sale_price = None if latest_line is None else Decimal(latest_line.unit_price)

    source_rows = db.execute(
        select(ProductBinding, SupplierProduct, Supplier, SupplierOfferState)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(
            ProductBinding.product_id == product_id,
            ProductBinding.status.in_(("active", "confirmed")),
        )
        .order_by(
            ProductBinding.is_primary.desc(),
            ProductBinding.priority,
            ProductBinding.id,
        )
    ).all()

    procurement_cost: Decimal | None = None
    source_name: str | None = None
    for _binding, supplier_product, supplier, state in source_rows:
        candidate: Decimal | None = None
        if state is not None and state.price is not None and state.available is not False:
            candidate = Decimal(state.price)
        elif supplier_product.current_price is not None and supplier_product.in_stock is not False:
            candidate = Decimal(supplier_product.current_price)
        if candidate is not None:
            procurement_cost = candidate
            source_name = supplier.name
            break

    commission_rate_pct = KASPI_COMMISSION_RATE * Decimal("100")
    tax_rate_pct = TAX_RATE * Decimal("100")
    profit_units_count = sum(int(line.quantity or 0) for line, _status in sale_lines)

    total_net_profit: Decimal | None = None
    total_net_margin_pct: Decimal | None = None
    if procurement_cost is not None:
        total_profit = Decimal("0")
        total_revenue = Decimal("0")
        for line, _status in sale_lines:
            economics = calculate_line_economics(
                unit_sale_price=Decimal(line.unit_price),
                quantity=int(line.quantity),
                procurement_unit_cost=procurement_cost,
            )
            total_profit += economics.net_profit
            total_revenue += economics.revenue
        total_net_profit = total_profit.quantize(Decimal("0.01"))
        if total_revenue > 0:
            total_net_margin_pct = (
                total_profit / total_revenue * Decimal("100")
            ).quantize(Decimal("0.01"))

    if sale_price is None:
        return ProductEconomicsRead(
            sale_unit_price=None,
            procurement_unit_cost=procurement_cost,
            procurement_source_name=source_name,
            kaspi_commission_rate_pct=commission_rate_pct,
            tax_rate_pct=tax_rate_pct,
            kaspi_commission=None,
            tax=None,
            logistics=None,
            net_profit=None,
            net_margin_pct=None,
            total_net_profit=total_net_profit,
            total_net_margin_pct=total_net_margin_pct,
            profit_units_count=profit_units_count,
        )

    fees = calculate_line_economics(
        unit_sale_price=sale_price,
        quantity=1,
        procurement_unit_cost=Decimal("0"),
    )
    if procurement_cost is None:
        return ProductEconomicsRead(
            sale_unit_price=sale_price,
            procurement_unit_cost=None,
            procurement_source_name=None,
            kaspi_commission_rate_pct=commission_rate_pct,
            tax_rate_pct=tax_rate_pct,
            kaspi_commission=fees.kaspi_commission,
            tax=fees.tax,
            logistics=fees.logistics,
            net_profit=None,
            net_margin_pct=None,
            total_net_profit=None,
            total_net_margin_pct=None,
            profit_units_count=profit_units_count,
        )

    economics = calculate_line_economics(
        unit_sale_price=sale_price,
        quantity=1,
        procurement_unit_cost=procurement_cost,
    )
    return ProductEconomicsRead(
        sale_unit_price=sale_price,
        procurement_unit_cost=procurement_cost,
        procurement_source_name=source_name,
        kaspi_commission_rate_pct=commission_rate_pct,
        tax_rate_pct=tax_rate_pct,
        kaspi_commission=economics.kaspi_commission,
        tax=economics.tax,
        logistics=economics.logistics,
        net_profit=economics.net_profit,
        net_margin_pct=economics.net_margin_pct,
        total_net_profit=total_net_profit,
        total_net_margin_pct=total_net_margin_pct,
        profit_units_count=profit_units_count,
    )