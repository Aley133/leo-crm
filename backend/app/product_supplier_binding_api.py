from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .models import Product
from .monitoring import BindingStatus, MonitorStatus, MonitorTarget
from .suppliers import ProductBinding, Supplier, SupplierProduct


class ManualSupplierBindingCreate(BaseModel):
    url: HttpUrl
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    is_primary: bool = False
    run_initial_check: bool = True


class ManualSupplierBindingResult(BaseModel):
    product_id: int
    supplier_code: str
    supplier_product_id: int
    binding_id: int
    monitor_target_id: int
    job_id: int | None
    created_supplier_product: bool
    created_binding: bool
    queued_initial_check: bool


router = APIRouter(
    prefix="/api/product-registry",
    tags=["product-supplier-binding"],
    dependencies=[Depends(require_service_token)],
)


def _source_from_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")

    if host == "ozon.ru" or host.endswith(".ozon.ru"):
        match = re.search(r"(?:product|context/detail/id)/(?:[^/]*-)?(\d+)(?:/|$)", path)
        external_id = match.group(1) if match else path.split("/")[-1]
        if not external_id:
            raise HTTPException(status_code=422, detail="Не удалось определить Ozon ID из ссылки")
        return "ozon", "Ozon", external_id

    if host in {"wildberries.ru", "www.wildberries.ru", "wb.ru"} or host.endswith(".wildberries.ru"):
        match = re.search(r"/catalog/(\d+)(?:/|$)", path)
        external_id = match.group(1) if match else path.split("/")[-1]
        if not external_id:
            raise HTTPException(status_code=422, detail="Не удалось определить WB ID из ссылки")
        return "wb", "Wildberries", external_id

    raise HTTPException(status_code=422, detail="Поддерживаются только ссылки Ozon и Wildberries")


@router.post(
    "/products/{product_id}/supplier-bindings/manual",
    response_model=ManualSupplierBindingResult,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_supplier_binding(
    product_id: int,
    payload: ManualSupplierBindingCreate,
    db: Session = Depends(get_db),
) -> ManualSupplierBindingResult:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    url = str(payload.url)
    supplier_code, supplier_name, external_id = _source_from_url(url)

    supplier = db.scalar(select(Supplier).where(Supplier.code == supplier_code).with_for_update())
    if supplier is None:
        supplier = Supplier(code=supplier_code, name=supplier_name, is_active=True)
        db.add(supplier)
        db.flush()

    supplier_product = db.scalar(
        select(SupplierProduct)
        .where(
            SupplierProduct.supplier_id == supplier.id,
            SupplierProduct.external_id == external_id,
        )
        .with_for_update()
    )
    created_supplier_product = supplier_product is None
    if supplier_product is None:
        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=external_id,
            title=(payload.title or product.name).strip(),
            url=url,
        )
        db.add(supplier_product)
        db.flush()
    else:
        supplier_product.url = url
        if payload.title:
            supplier_product.title = payload.title.strip()

    binding = db.scalar(
        select(ProductBinding)
        .where(
            ProductBinding.product_id == product.id,
            ProductBinding.supplier_product_id == supplier_product.id,
        )
        .with_for_update()
    )
    created_binding = binding is None
    if binding is None:
        binding = ProductBinding(
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            status=BindingStatus.ACTIVE.value,
            decision_source="manual",
            is_primary=payload.is_primary,
            confidence_score=100,
            priority=0 if payload.is_primary else 100,
            confirmed_at=datetime.now(UTC),
        )
        db.add(binding)
        db.flush()
    else:
        binding.status = BindingStatus.ACTIVE.value
        binding.decision_source = "manual"
        binding.confirmed_at = binding.confirmed_at or datetime.now(UTC)
        if payload.is_primary:
            binding.is_primary = True
            binding.priority = 0

    if payload.is_primary:
        other_bindings = db.scalars(
            select(ProductBinding).where(
                ProductBinding.product_id == product.id,
                ProductBinding.id != binding.id,
                ProductBinding.is_primary.is_(True),
            )
        ).all()
        for other in other_bindings:
            other.is_primary = False

    monitor_target = db.scalar(
        select(MonitorTarget)
        .where(MonitorTarget.product_binding_id == binding.id)
        .with_for_update()
    )
    if monitor_target is None:
        monitor_target = MonitorTarget(
            product_binding_id=binding.id,
            status=MonitorStatus.ACTIVE.value,
            interval_seconds=300,
            next_check_at=datetime.now(UTC),
        )
        db.add(monitor_target)
        db.flush()
    else:
        monitor_target.status = MonitorStatus.ACTIVE.value
        monitor_target.next_check_at = datetime.now(UTC)

    job: BrowserAgentJob | None = None
    if payload.run_initial_check:
        job = BrowserAgentJob(
            monitor_target_id=monitor_target.id,
            supplier_product_id=supplier_product.id,
            url=supplier_product.url,
            status=BrowserAgentJobStatus.QUEUED.value,
        )
        db.add(job)
        db.flush()

    db.commit()

    return ManualSupplierBindingResult(
        product_id=product.id,
        supplier_code=supplier.code,
        supplier_product_id=supplier_product.id,
        binding_id=binding.id,
        monitor_target_id=monitor_target.id,
        job_id=None if job is None else job.id,
        created_supplier_product=created_supplier_product,
        created_binding=created_binding,
        queued_initial_check=job is not None,
    )
