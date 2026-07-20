from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import MarketplaceOrderLine, Product, ProductStatus
from .monitoring import MonitorTarget, SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ProductRegistryRow(BaseModel):
    product_id: int
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None
    status: str
    orders_count: int
    units_sold: int
    revenue_kzt: Decimal
    supplier_count: int
    active_monitor_count: int
    available_offer_count: int
    failed_monitor_count: int
    best_supplier_name: str | None
    best_supplier_price: Decimal | None
    best_supplier_currency: str | None
    last_checked_at: datetime | None


class ProductRegistryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    brand: str | None = Field(default=None, max_length=255)
    merchant_sku: str | None = Field(default=None, max_length=128)
    status: ProductStatus | None = None


router = APIRouter(
    prefix="/api/product-registry",
    tags=["product-registry"],
    dependencies=[Depends(require_service_token)],
)


def _product_rows(db: Session, products: list[Product]) -> list[ProductRegistryRow]:
    if not products:
        return []
    ids = [item.id for item in products]

    sales = {
        row.product_id: row
        for row in db.execute(
            select(
                MarketplaceOrderLine.product_id,
                func.count(MarketplaceOrderLine.id).label("orders_count"),
                func.coalesce(func.sum(MarketplaceOrderLine.quantity), 0).label("units_sold"),
                func.coalesce(func.sum(MarketplaceOrderLine.line_total), 0).label("revenue_kzt"),
            )
            .where(MarketplaceOrderLine.product_id.in_(ids))
            .group_by(MarketplaceOrderLine.product_id)
        )
    }

    binding_rows = db.execute(
        select(
            ProductBinding.product_id,
            Supplier.name,
            SupplierOfferState.price,
            SupplierOfferState.currency,
            SupplierOfferState.available,
            SupplierOfferState.last_checked_at,
            MonitorTarget.id,
            MonitorTarget.status,
            MonitorTarget.consecutive_failures,
        )
        .select_from(ProductBinding)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(SupplierOfferState, SupplierOfferState.supplier_product_id == SupplierProduct.id)
        .outerjoin(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .where(ProductBinding.product_id.in_(ids))
        .order_by(ProductBinding.product_id, ProductBinding.is_primary.desc(), ProductBinding.priority)
    ).all()

    grouped: dict[int, list] = {item.id: [] for item in products}
    for row in binding_rows:
        grouped[row[0]].append(row)

    result: list[ProductRegistryRow] = []
    for product in products:
        rows = grouped[product.id]
        available = [row for row in rows if row[4] is True]
        priced = [row for row in available if row[2] is not None]
        best = min(priced, key=lambda row: row[2]) if priced else (available[0] if available else None)
        sale = sales.get(product.id)
        checks = [row[5] for row in rows if row[5] is not None]
        result.append(
            ProductRegistryRow(
                product_id=product.id,
                kaspi_product_id=product.kaspi_product_id,
                merchant_sku=product.merchant_sku,
                name=product.name,
                brand=product.brand,
                status=product.status,
                orders_count=int(sale.orders_count) if sale else 0,
                units_sold=int(sale.units_sold) if sale else 0,
                revenue_kzt=Decimal(sale.revenue_kzt) if sale else Decimal("0"),
                supplier_count=len(rows),
                active_monitor_count=sum(1 for row in rows if row[6] is not None and row[7] == "active"),
                available_offer_count=len(available),
                failed_monitor_count=sum(1 for row in rows if (row[8] or 0) > 0),
                best_supplier_name=best[1] if best else None,
                best_supplier_price=best[2] if best else None,
                best_supplier_currency=best[3] if best else None,
                last_checked_at=max(checks) if checks else None,
            )
        )
    return result


@router.get("/products", response_model=list[ProductRegistryRow])
def list_products(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    status: ProductStatus | None = None,
    only_without_supplier: bool = False,
    only_failures: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ProductRegistryRow]:
    statement = select(Product).order_by(Product.id).offset(offset).limit(limit)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(or_(
            Product.name.ilike(pattern),
            Product.kaspi_product_id.ilike(pattern),
            Product.merchant_sku.ilike(pattern),
            Product.brand.ilike(pattern),
        ))
    if status is not None:
        statement = statement.where(Product.status == status.value)

    rows = _product_rows(db, list(db.scalars(statement).all()))
    if only_without_supplier:
        rows = [row for row in rows if row.supplier_count == 0]
    if only_failures:
        rows = [row for row in rows if row.failed_monitor_count > 0]
    return rows


@router.get("/products/{product_id}", response_model=ProductRegistryRow)
def read_product(product_id: int, db: Session = Depends(get_db)) -> ProductRegistryRow:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_rows(db, [product])[0]


@router.patch("/products/{product_id}", response_model=ProductRegistryRow)
def update_product(
    product_id: int,
    payload: ProductRegistryUpdate,
    db: Session = Depends(get_db),
) -> ProductRegistryRow:
    product = db.get(Product, product_id)
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
