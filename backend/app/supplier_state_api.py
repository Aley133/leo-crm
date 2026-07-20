from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import Product
from .monitoring import MonitorStatus, MonitorTarget, SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


class SupplierStateSummary(BaseModel):
    total_products: int
    bound_products: int
    monitored_bindings: int
    offers_with_state: int
    available_offers: int
    unavailable_offers: int
    stale_offers: int
    degraded_targets: int
    failed_targets: int


class SupplierStateRow(BaseModel):
    product_id: int
    kaspi_product_id: str
    product_name: str
    brand: str | None
    binding_id: int
    binding_status: str
    is_primary: bool
    supplier_id: int
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
    is_stale: bool


router = APIRouter(
    prefix="/api/supplier-state",
    tags=["supplier-state"],
    dependencies=[Depends(require_service_token)],
)


def _stale_before(stale_after_minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=stale_after_minutes)


@router.get("/summary", response_model=SupplierStateSummary)
def get_supplier_state_summary(
    stale_after_minutes: int = Query(default=30, ge=5, le=10_080),
    db: Session = Depends(get_db),
) -> SupplierStateSummary:
    stale_before = _stale_before(stale_after_minutes)

    total_products = db.scalar(select(func.count()).select_from(Product)) or 0
    bound_products = db.scalar(
        select(func.count(func.distinct(ProductBinding.product_id))).select_from(ProductBinding)
    ) or 0
    monitored_bindings = db.scalar(select(func.count()).select_from(MonitorTarget)) or 0
    offers_with_state = db.scalar(select(func.count()).select_from(SupplierOfferState)) or 0
    available_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.available.is_(True))
    ) or 0
    unavailable_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.available.is_(False))
    ) or 0
    stale_offers = db.scalar(
        select(func.count())
        .select_from(SupplierOfferState)
        .where(SupplierOfferState.last_checked_at < stale_before)
    ) or 0
    degraded_targets = db.scalar(
        select(func.count())
        .select_from(MonitorTarget)
        .where(MonitorTarget.status.in_([MonitorStatus.DEGRADED.value, MonitorStatus.MANUAL_REVIEW.value]))
    ) or 0
    failed_targets = db.scalar(
        select(func.count())
        .select_from(MonitorTarget)
        .where(MonitorTarget.consecutive_failures > 0)
    ) or 0

    return SupplierStateSummary(
        total_products=total_products,
        bound_products=bound_products,
        monitored_bindings=monitored_bindings,
        offers_with_state=offers_with_state,
        available_offers=available_offers,
        unavailable_offers=unavailable_offers,
        stale_offers=stale_offers,
        degraded_targets=degraded_targets,
        failed_targets=failed_targets,
    )


@router.get("/offers", response_model=list[SupplierStateRow])
def list_supplier_state_offers(
    q: str | None = Query(default=None, min_length=1, max_length=200),
    supplier_code: str | None = Query(default=None, min_length=1, max_length=64),
    availability: bool | None = None,
    only_stale: bool = False,
    only_failures: bool = False,
    stale_after_minutes: int = Query(default=30, ge=5, le=10_080),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[SupplierStateRow]:
    stale_before = _stale_before(stale_after_minutes)
    stale_expression = or_(
        SupplierOfferState.last_checked_at.is_(None),
        SupplierOfferState.last_checked_at < stale_before,
    )

    statement = (
        select(
            Product.id,
            Product.kaspi_product_id,
            Product.name,
            Product.brand,
            ProductBinding.id,
            ProductBinding.status,
            ProductBinding.is_primary,
            Supplier.id,
            Supplier.code,
            Supplier.name,
            SupplierProduct.id,
            SupplierProduct.title,
            SupplierProduct.url,
            MonitorTarget.id,
            MonitorTarget.status,
            func.coalesce(MonitorTarget.consecutive_failures, 0),
            SupplierOfferState.price,
            SupplierOfferState.old_price,
            SupplierOfferState.currency,
            SupplierOfferState.available,
            SupplierOfferState.stock,
            SupplierOfferState.delivery_days,
            SupplierOfferState.seller,
            SupplierOfferState.observed_at,
            SupplierOfferState.last_checked_at,
            case((stale_expression, True), else_=False),
        )
        .select_from(ProductBinding)
        .join(Product, Product.id == ProductBinding.product_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
    )

    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Product.name.ilike(pattern),
                Product.kaspi_product_id.ilike(pattern),
                Product.brand.ilike(pattern),
                SupplierProduct.title.ilike(pattern),
            )
        )
    if supplier_code:
        statement = statement.where(Supplier.code == supplier_code.strip().lower())
    if availability is not None:
        statement = statement.where(SupplierOfferState.available.is_(availability))
    if only_stale:
        statement = statement.where(stale_expression)
    if only_failures:
        statement = statement.where(MonitorTarget.consecutive_failures > 0)

    rows = db.execute(
        statement
        .order_by(Product.id, ProductBinding.is_primary.desc(), ProductBinding.priority, ProductBinding.id)
        .offset(offset)
        .limit(limit)
    ).all()

    return [
        SupplierStateRow(
            product_id=row[0],
            kaspi_product_id=row[1],
            product_name=row[2],
            brand=row[3],
            binding_id=row[4],
            binding_status=row[5],
            is_primary=row[6],
            supplier_id=row[7],
            supplier_code=row[8],
            supplier_name=row[9],
            supplier_product_id=row[10],
            supplier_product_title=row[11],
            supplier_product_url=row[12],
            monitor_target_id=row[13],
            monitor_status=row[14],
            consecutive_failures=row[15],
            price=row[16],
            old_price=row[17],
            currency=row[18],
            available=row[19],
            stock=row[20],
            delivery_days=row[21],
            seller=row[22],
            observed_at=row[23],
            last_checked_at=row[24],
            is_stale=row[25],
        )
        for row in rows
    ]
