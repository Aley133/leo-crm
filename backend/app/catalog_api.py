from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import Product
from .monitoring import MonitorTarget, SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


class CatalogProductRow(BaseModel):
    product_id: int
    kaspi_product_id: str
    merchant_sku: str | None
    product_name: str
    brand: str | None
    product_status: str
    supplier_count: int
    monitored_count: int
    failed_monitor_count: int
    available_offer_count: int
    best_supplier_name: str | None
    best_supplier_code: str | None
    best_supplier_price: Decimal | None
    best_supplier_currency: str | None
    last_checked_at: datetime | None


class SupplierOfferRow(BaseModel):
    supplier_product_id: int
    supplier_id: int
    supplier_code: str
    supplier_name: str
    external_id: str
    supplier_product_title: str
    supplier_product_url: str
    product_id: int | None
    kaspi_product_id: str | None
    kaspi_product_name: str | None
    binding_status: str | None
    is_primary: bool
    confidence_score: int | None
    price: Decimal | None
    currency: str | None
    available: bool | None
    delivery_days: int | None
    seller: str | None
    monitor_status: str | None
    consecutive_failures: int
    last_checked_at: datetime | None


router = APIRouter(
    prefix="/api/catalog",
    tags=["catalog"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/products", response_model=list[CatalogProductRow])
def list_catalog_products(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    only_without_supplier: bool = False,
    only_failures: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[CatalogProductRow]:
    statement = select(Product).order_by(Product.id).offset(offset).limit(limit)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Product.name.ilike(pattern),
                Product.kaspi_product_id.ilike(pattern),
                Product.merchant_sku.ilike(pattern),
                Product.brand.ilike(pattern),
            )
        )

    products = list(db.scalars(statement).all())
    if not products:
        return []

    product_ids = [product.id for product in products]
    binding_rows = db.execute(
        select(
            ProductBinding.product_id,
            ProductBinding.id,
            ProductBinding.is_primary,
            Supplier.code,
            Supplier.name,
            SupplierOfferState.price,
            SupplierOfferState.currency,
            SupplierOfferState.available,
            SupplierOfferState.last_checked_at,
            MonitorTarget.id,
            MonitorTarget.consecutive_failures,
        )
        .select_from(ProductBinding)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(SupplierOfferState, SupplierOfferState.supplier_product_id == SupplierProduct.id)
        .outerjoin(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .where(ProductBinding.product_id.in_(product_ids))
        .order_by(ProductBinding.product_id, ProductBinding.is_primary.desc(), ProductBinding.priority)
    ).all()

    grouped: dict[int, list] = {product_id: [] for product_id in product_ids}
    for row in binding_rows:
        grouped[row[0]].append(row)

    result: list[CatalogProductRow] = []
    for product in products:
        rows = grouped[product.id]
        available_rows = [row for row in rows if row[7] is True]
        priced_rows = [row for row in available_rows if row[5] is not None]
        best = min(priced_rows, key=lambda row: row[5]) if priced_rows else (available_rows[0] if available_rows else None)
        failed_count = sum(1 for row in rows if (row[10] or 0) > 0)

        if only_without_supplier and rows:
            continue
        if only_failures and failed_count == 0:
            continue

        checked_values = [row[8] for row in rows if row[8] is not None]
        result.append(
            CatalogProductRow(
                product_id=product.id,
                kaspi_product_id=product.kaspi_product_id,
                merchant_sku=product.merchant_sku,
                product_name=product.name,
                brand=product.brand,
                product_status=product.status,
                supplier_count=len(rows),
                monitored_count=sum(1 for row in rows if row[9] is not None),
                failed_monitor_count=failed_count,
                available_offer_count=len(available_rows),
                best_supplier_name=best[4] if best else None,
                best_supplier_code=best[3] if best else None,
                best_supplier_price=best[5] if best else None,
                best_supplier_currency=best[6] if best else None,
                last_checked_at=max(checked_values) if checked_values else None,
            )
        )
    return result


@router.get("/supplier-offers", response_model=list[SupplierOfferRow])
def list_catalog_supplier_offers(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    supplier_code: str | None = Query(default=None, min_length=1, max_length=64),
    availability: bool | None = None,
    only_unbound: bool = False,
    only_failures: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[SupplierOfferRow]:
    statement = (
        select(
            SupplierProduct.id,
            Supplier.id,
            Supplier.code,
            Supplier.name,
            SupplierProduct.external_id,
            SupplierProduct.title,
            SupplierProduct.url,
            Product.id,
            Product.kaspi_product_id,
            Product.name,
            ProductBinding.status,
            ProductBinding.is_primary,
            ProductBinding.confidence_score,
            SupplierOfferState.price,
            SupplierOfferState.currency,
            SupplierOfferState.available,
            SupplierOfferState.delivery_days,
            SupplierOfferState.seller,
            MonitorTarget.status,
            MonitorTarget.consecutive_failures,
            SupplierOfferState.last_checked_at,
        )
        .select_from(SupplierProduct)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .outerjoin(Product, Product.id == ProductBinding.product_id)
        .outerjoin(SupplierOfferState, SupplierOfferState.supplier_product_id == SupplierProduct.id)
        .outerjoin(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
    )
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                SupplierProduct.title.ilike(pattern),
                SupplierProduct.external_id.ilike(pattern),
                Product.name.ilike(pattern),
                Product.kaspi_product_id.ilike(pattern),
            )
        )
    if supplier_code:
        statement = statement.where(Supplier.code == supplier_code.strip().lower())
    if availability is not None:
        statement = statement.where(SupplierOfferState.available.is_(availability))
    if only_unbound:
        statement = statement.where(ProductBinding.id.is_(None))
    if only_failures:
        statement = statement.where(MonitorTarget.consecutive_failures > 0)

    rows = db.execute(statement.order_by(Supplier.id, SupplierProduct.id).offset(offset).limit(limit)).all()
    return [
        SupplierOfferRow(
            supplier_product_id=row[0], supplier_id=row[1], supplier_code=row[2], supplier_name=row[3],
            external_id=row[4], supplier_product_title=row[5], supplier_product_url=row[6],
            product_id=row[7], kaspi_product_id=row[8], kaspi_product_name=row[9], binding_status=row[10],
            is_primary=bool(row[11]), confidence_score=row[12], price=row[13], currency=row[14],
            available=row[15], delivery_days=row[16], seller=row[17], monitor_status=row[18],
            consecutive_failures=row[19] or 0, last_checked_at=row[20],
        )
        for row in rows
    ]
