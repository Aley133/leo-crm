from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .models import Product
from .monitoring import BindingStatus, SupplierOfferState
from .offer_contracts import offer_fingerprint
from .suppliers import ProductBinding, Supplier, SupplierProduct


class FixedSourceType(StrEnum):
    OFFLINE = "offline"
    PRODUCTION = "production"


class FixedProcurementSourceUpsert(BaseModel):
    source_type: FixedSourceType
    source_name: str = Field(min_length=2, max_length=255)
    price: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    delivery_days: int = Field(default=0, ge=0, le=365)
    is_primary: bool = True


class FixedProcurementSourceRead(BaseModel):
    supplier_id: int
    supplier_product_id: int
    binding_id: int
    source_type: FixedSourceType
    source_name: str
    price: Decimal
    delivery_days: int
    is_primary: bool
    monitoring_enabled: bool = False


router = APIRouter(
    prefix="/api/products",
    tags=["fixed-procurement-sources"],
    dependencies=[Depends(require_service_token)],
)


def _supplier_code(source_type: FixedSourceType, source_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", source_name.casefold()).strip("-")
    suffix = hashlib.sha1(source_name.strip().casefold().encode("utf-8")).hexdigest()[:10]
    readable = normalized[:36] or "source"
    return f"{source_type.value}-{readable}-{suffix}"


@router.post(
    "/{product_id}/fixed-procurement-source",
    response_model=FixedProcurementSourceRead,
    status_code=status.HTTP_200_OK,
)
def upsert_fixed_procurement_source(
    product_id: int,
    payload: FixedProcurementSourceUpsert,
    db: Session = Depends(get_db),
) -> FixedProcurementSourceRead:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    code = _supplier_code(payload.source_type, payload.source_name)
    supplier = db.scalar(select(Supplier).where(Supplier.code == code))
    if supplier is None:
        supplier = Supplier(code=code, name=payload.source_name.strip(), is_active=True)
        db.add(supplier)
        db.flush()
    else:
        supplier.name = payload.source_name.strip()
        supplier.is_active = True

    external_id = f"fixed:{product_id}"
    supplier_product = db.scalar(
        select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier.id,
            SupplierProduct.external_id == external_id,
        )
    )
    if supplier_product is None:
        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=external_id,
            title=product.name,
            url=f"manual://{payload.source_type.value}/{supplier.id}/{product_id}",
            current_price=payload.price,
            delivery_days=payload.delivery_days,
            in_stock=True,
            last_checked_at=None,
        )
        db.add(supplier_product)
        db.flush()
    else:
        supplier_product.title = product.name
        supplier_product.current_price = payload.price
        supplier_product.delivery_days = payload.delivery_days
        supplier_product.in_stock = True

    binding = db.scalar(
        select(ProductBinding).where(
            ProductBinding.product_id == product_id,
            ProductBinding.supplier_product_id == supplier_product.id,
        )
    )
    now = datetime.now(UTC)
    if binding is None:
        binding = ProductBinding(
            product_id=product_id,
            supplier_product_id=supplier_product.id,
            status=BindingStatus.ACTIVE.value,
            decision_source="manual",
            is_primary=payload.is_primary,
            confidence_score=100,
            priority=0 if payload.is_primary else 100,
            confirmed_at=now,
            last_validated_at=now,
        )
        db.add(binding)
        db.flush()
    else:
        binding.status = BindingStatus.ACTIVE.value
        binding.decision_source = "manual"
        binding.is_primary = payload.is_primary
        binding.confidence_score = 100
        binding.priority = 0 if payload.is_primary else binding.priority
        binding.confirmed_at = binding.confirmed_at or now
        binding.last_validated_at = now

    if payload.is_primary:
        for other in db.scalars(
            select(ProductBinding).where(
                ProductBinding.product_id == product_id,
                ProductBinding.id != binding.id,
            )
        ):
            other.is_primary = False

    state = db.scalar(
        select(SupplierOfferState).where(
            SupplierOfferState.supplier_product_id == supplier_product.id
        )
    )
    fingerprint = offer_fingerprint(
        supplier_product_id=supplier_product.id,
        price=payload.price,
        currency="KZT",
        available=True,
        stock=None,
        delivery_days=payload.delivery_days,
        seller=supplier.name,
        adapter_schema_version="fixed-source-v1",
    )
    if state is None:
        state = SupplierOfferState(
            supplier_product_id=supplier_product.id,
            price=payload.price,
            old_price=None,
            currency="KZT",
            available=True,
            stock=None,
            delivery_days=payload.delivery_days,
            seller=supplier.name,
            fingerprint=fingerprint,
            adapter_schema_version="fixed-source-v1",
            observed_at=now,
            last_checked_at=now,
            version=1,
        )
        db.add(state)
    else:
        if state.price != payload.price:
            state.old_price = state.price
        state.price = payload.price
        state.currency = "KZT"
        state.available = True
        state.delivery_days = payload.delivery_days
        state.seller = supplier.name
        state.fingerprint = fingerprint
        state.adapter_schema_version = "fixed-source-v1"
        state.observed_at = now
        state.last_checked_at = now
        state.version += 1

    db.commit()
    db.refresh(binding)

    return FixedProcurementSourceRead(
        supplier_id=supplier.id,
        supplier_product_id=supplier_product.id,
        binding_id=binding.id,
        source_type=payload.source_type,
        source_name=supplier.name,
        price=payload.price,
        delivery_days=payload.delivery_days,
        is_primary=binding.is_primary,
    )
