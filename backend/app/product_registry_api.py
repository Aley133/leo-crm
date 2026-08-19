from __future__ import annotations

import asyncio
import time
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_models import DumpingPolicy
from .dumping_service import physical_stock_counts, set_product_sale_enabled
from .models import MarketplaceOrderLine, Product, ProductStatus
from .monitoring import MonitorTarget, SupplierOfferState
from .kaspi_product_photo import fetch_kaspi_product_photo
from .product_images import normalize_product_image_url
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ProductRegistryRow(BaseModel):
    product_id: int
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None
    image_url: str | None
    status: str
    sale_enabled: bool
    inventory_on_hand: int
    dumping_enabled: bool
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


class ProductSaleStateUpdate(BaseModel):
    sale_enabled: bool


class ProductImageResolution(BaseModel):
    product_id: int
    image_url: str
    cached: bool


router = APIRouter(
    prefix="/api/product-registry",
    tags=["product-registry"],
    dependencies=[Depends(require_service_token)],
)

_VISIBLE_BINDING_STATUSES = ("active", "confirmed", "degraded")
_IMAGE_RESOLVE_SEMAPHORE = asyncio.Semaphore(1)
_IMAGE_RESOLVE_LOCKS: dict[int, asyncio.Lock] = {}
_IMAGE_FAILURE_NOT_BEFORE: dict[int, float] = {}
_IMAGE_FAILURE_COOLDOWN_SECONDS = 10 * 60


def _product_rows(db: Session, products: list[Product]) -> list[ProductRegistryRow]:
    if not products:
        return []
    ids = [item.id for item in products]
    stock_counts = physical_stock_counts(db, product_ids=set(ids))
    dumping_enabled_ids = {
        int(product_id)
        for product_id in db.scalars(
            select(DumpingPolicy.product_id).where(
                DumpingPolicy.product_id.in_(ids),
                DumpingPolicy.enabled.is_(True),
                DumpingPolicy.auto_publish_xml.is_(True),
            )
        ).all()
    }

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
        .where(
            ProductBinding.product_id.in_(ids),
            ProductBinding.status.in_(_VISIBLE_BINDING_STATUSES),
        )
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
                image_url=product.image_url,
                status=product.status,
                sale_enabled=bool(product.sale_enabled),
                inventory_on_hand=stock_counts.get(int(product.id), 0),
                dumping_enabled=int(product.id) in dumping_enabled_ids,
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
    sale_enabled: bool | None = None,
    only_without_supplier: bool = False,
    only_failures: bool = False,
    only_monitored: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ProductRegistryRow]:
    statement = select(Product)
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
    if sale_enabled is not None:
        statement = statement.where(Product.sale_enabled.is_(sale_enabled))

    visible_binding_exists = exists(
        select(ProductBinding.id).where(
            ProductBinding.product_id == Product.id,
            ProductBinding.status.in_(_VISIBLE_BINDING_STATUSES),
        )
    )
    if only_without_supplier:
        statement = statement.where(~visible_binding_exists)
    if only_failures:
        statement = statement.where(
            exists(
                select(MonitorTarget.id)
                .join(
                    ProductBinding,
                    ProductBinding.id == MonitorTarget.product_binding_id,
                )
                .where(
                    ProductBinding.product_id == Product.id,
                    ProductBinding.status.in_(_VISIBLE_BINDING_STATUSES),
                    MonitorTarget.consecutive_failures > 0,
                )
            )
        )
    if only_monitored:
        statement = statement.where(
            exists(
                select(MonitorTarget.id)
                .join(
                    ProductBinding,
                    ProductBinding.id == MonitorTarget.product_binding_id,
                )
                .where(
                    ProductBinding.product_id == Product.id,
                    ProductBinding.status.in_(_VISIBLE_BINDING_STATUSES),
                    MonitorTarget.status == "active",
                )
            )
        )

    statement = statement.order_by(Product.id).offset(offset).limit(limit)
    return _product_rows(db, list(db.scalars(statement).all()))


@router.get("/products/{product_id}", response_model=ProductRegistryRow)
def read_product(product_id: int, db: Session = Depends(get_db)) -> ProductRegistryRow:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_rows(db, [product])[0]


@router.post("/products/{product_id}/resolve-image", response_model=ProductImageResolution)
async def resolve_product_image(product_id: int, db: Session = Depends(get_db)) -> ProductImageResolution:
    """Resolve one missing Kaspi photo from public-card HTML without an Agent.

    The browser calls this endpoint only when a missing-photo placeholder enters
    the viewport. A per-product lock deduplicates concurrent order lines, the
    global semaphore bounds Kaspi traffic, and a successful URL is persisted so
    future screens use the database without another Kaspi request.
    """

    lock = _IMAGE_RESOLVE_LOCKS.setdefault(product_id, asyncio.Lock())
    async with lock:
        product = db.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        current_image = normalize_product_image_url(product.image_url)
        if current_image:
            return ProductImageResolution(product_id=product.id, image_url=current_image, cached=True)

        now = time.monotonic()
        retry_at = _IMAGE_FAILURE_NOT_BEFORE.get(product_id, 0.0)
        if retry_at > now:
            retry_after = max(1, int(retry_at - now))
            raise HTTPException(
                status_code=429,
                detail=f"Повторное чтение фотографии доступно через {retry_after} сек.",
                headers={"Retry-After": str(retry_after)},
            )

        kaspi_product_id = str(product.kaspi_product_id or "").strip()
        if not kaspi_product_id:
            raise HTTPException(status_code=422, detail="У товара отсутствует Kaspi product ID")
        product_name = str(product.name or "").strip()

        # Release the database connection before the external HTTP operation.
        db.commit()
        try:
            async with _IMAGE_RESOLVE_SEMAPHORE:
                async with asyncio.timeout(40):
                    resolved_image = await fetch_kaspi_product_photo(
                        kaspi_product_id=kaspi_product_id,
                        product_name=product_name,
                        city_id="196220100",
                    )
            image_url = normalize_product_image_url(resolved_image)
            if not image_url:
                raise ValueError("в HTML публичной карточки отсутствует допустимый og:image")
        except Exception as exc:
            _IMAGE_FAILURE_NOT_BEFORE[product_id] = time.monotonic() + _IMAGE_FAILURE_COOLDOWN_SECONDS
            error_detail = str(exc).strip() or type(exc).__name__
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось получить фотографию Kaspi: {error_detail[:500]}",
            ) from exc

        db.expire_all()
        product = db.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        product.image_url = image_url
        db.commit()
        _IMAGE_FAILURE_NOT_BEFORE.pop(product_id, None)
        return ProductImageResolution(product_id=product.id, image_url=image_url, cached=False)


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


@router.patch("/products/{product_id}/sale-state", response_model=ProductRegistryRow)
def update_product_sale_state(
    product_id: int,
    payload: ProductSaleStateUpdate,
    db: Session = Depends(get_db),
) -> ProductRegistryRow:
    try:
        set_product_sale_enabled(
            db,
            product_id=product_id,
            sale_enabled=payload.sale_enabled,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_rows(db, [product])[0]
