from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .commerce.profit_calculator import (
    KASPI_COMMISSION_RATE,
    TAX_RATE,
    calculate_line_economics,
)
from .db import get_db
from .inventory_models import InventoryAllocation
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
    line_ids = [line.id for line, _status in sale_lines]

    inventory_by_line: dict[int, tuple[int, Decimal]] = {}
    if line_ids:
        inventory_rows = db.execute(
            select(
                InventoryAllocation.marketplace_order_line_id,
                func.sum(InventoryAllocation.quantity),
                func.sum(InventoryAllocation.quantity * InventoryAllocation.unit_cost),
            )
            .where(InventoryAllocation.marketplace_order_line_id.in_(line_ids))
            .group_by(InventoryAllocation.marketplace_order_line_id)
        ).all()
        inventory_by_line = {
            int(line_id): (int(quantity or 0), Decimal(total_cost or 0))
            for line_id, quantity, total_cost in inventory_rows
        }

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

    current_source_cost: Decimal | None = None
    current_source_name: str | None = None
    for _binding, supplier_product, supplier, state in source_rows:
        candidate: Decimal | None = None
        if state is not None and state.price is not None and state.available is not False:
            candidate = Decimal(state.price)
        elif supplier_product.current_price is not None and supplier_product.in_stock is not False:
            candidate = Decimal(supplier_product.current_price)
        if candidate is not None:
            current_source_cost = candidate
            current_source_name = supplier.name
            break

    procurement_cost = current_source_cost
    source_name = current_source_name
    if latest_line is not None:
        allocated_quantity, allocated_cost = inventory_by_line.get(latest_line.id, (0, Decimal("0")))
        if allocated_quantity > 0:
            procurement_cost = (allocated_cost / Decimal(allocated_quantity)).quantize(Decimal("0.01"))
            source_name = "Склад FIFO"

    commission_rate_pct = KASPI_COMMISSION_RATE * Decimal("100")
    tax_rate_pct = TAX_RATE * Decimal("100")

    # Realized aggregate: count only units with a known, defensible cost.
    # FIFO allocations are authoritative. A configured procurement source may
    # cost the remaining units, but unknown units never suppress known profit.
    total_profit = Decimal("0")
    total_revenue = Decimal("0")
    profit_units_count = 0
    for line, _status in sale_lines:
        line_quantity = max(int(line.quantity or 0), 0)
        if line_quantity <= 0:
            continue

        allocated_quantity, allocated_cost = inventory_by_line.get(line.id, (0, Decimal("0")))
        allocated_quantity = min(max(allocated_quantity, 0), line_quantity)

        if allocated_quantity > 0:
            fifo_unit_cost = (allocated_cost / Decimal(allocated_quantity)).quantize(Decimal("0.01"))
            economics = calculate_line_economics(
                unit_sale_price=Decimal(line.unit_price),
                quantity=allocated_quantity,
                procurement_unit_cost=fifo_unit_cost,
            )
            total_profit += economics.net_profit
            total_revenue += economics.revenue
            profit_units_count += allocated_quantity

        remaining_quantity = line_quantity - allocated_quantity
        if remaining_quantity > 0 and current_source_cost is not None:
            economics = calculate_line_economics(
                unit_sale_price=Decimal(line.unit_price),
                quantity=remaining_quantity,
                procurement_unit_cost=current_source_cost,
            )
            total_profit += economics.net_profit
            total_revenue += economics.revenue
            profit_units_count += remaining_quantity

    total_net_profit: Decimal | None = None
    total_net_margin_pct: Decimal | None = None
    if profit_units_count > 0:
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
            total_net_profit=total_net_profit,
            total_net_margin_pct=total_net_margin_pct,
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
