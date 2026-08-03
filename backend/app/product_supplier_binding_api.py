from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_dispatch import queue_browser_target_now
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .db import get_db
from .models import Product
from .monitoring import (
    BindingStatus,
    MonitorStatus,
    MonitorTarget,
    SupplierOfferState,
)
from .supplier_identity import (
    UnsupportedSupplierUrl,
    canonical_supplier_product_identity,
    parse_supplier_url,
)
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


class ManualSupplierBindingUpdate(BaseModel):
    url: HttpUrl
    run_initial_check: bool = True


class ManualSupplierBindingUpdateResult(BaseModel):
    product_id: int
    binding_id: int
    monitor_target_id: int
    previous_supplier_product_id: int
    supplier_product_id: int
    supplier_code: str
    url: str
    created_supplier_product: bool
    cancelled_stale_jobs: int
    job_id: int | None
    queued_initial_check: bool


router = APIRouter(
    prefix="/api/product-registry",
    tags=["product-supplier-binding"],
    dependencies=[Depends(require_service_token)],
)


def _source_from_url(url: str) -> tuple[str, str, str]:
    try:
        identity = parse_supplier_url(url)
    except UnsupportedSupplierUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return (
        identity.supplier_code,
        identity.supplier_name,
        identity.external_id,
    )


def _checked_timestamp(
    state: SupplierOfferState | None,
    supplier_product: SupplierProduct,
) -> datetime:
    checked_at = (
        state.last_checked_at
        if state is not None
        else supplier_product.last_checked_at
    )
    if checked_at is None:
        checked_at = supplier_product.created_at
    if checked_at is None:
        return datetime.min.replace(tzinfo=UTC)
    if checked_at.tzinfo is None:
        return checked_at.replace(tzinfo=UTC)
    return checked_at.astimezone(UTC)


def _matching_supplier_products(
    db: Session,
    *,
    supplier: Supplier,
    external_id: str,
) -> list[tuple[SupplierProduct, SupplierOfferState | None]]:
    rows = db.execute(
        select(SupplierProduct, SupplierOfferState)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(SupplierProduct.supplier_id == supplier.id)
        .with_for_update(of=SupplierProduct)
    ).all()
    matches = [
        (supplier_product, state)
        for supplier_product, state in rows
        if canonical_supplier_product_identity(
            supplier_code=supplier.code,
            external_id=supplier_product.external_id,
            url=supplier_product.url,
        )
        == external_id.casefold()
    ]
    return sorted(
        matches,
        key=lambda row: (
            _checked_timestamp(row[1], row[0]),
            row[0].external_id == external_id,
            row[0].id,
        ),
        reverse=True,
    )


def _disable_duplicate_bindings(
    db: Session,
    *,
    product_id: int,
    supplier: Supplier,
    external_id: str,
    winner_supplier_product_id: int,
) -> None:
    rows = db.execute(
        select(ProductBinding, SupplierProduct, MonitorTarget)
        .join(
            SupplierProduct,
            SupplierProduct.id == ProductBinding.supplier_product_id,
        )
        .outerjoin(
            MonitorTarget,
            MonitorTarget.product_binding_id == ProductBinding.id,
        )
        .where(
            ProductBinding.product_id == product_id,
            SupplierProduct.supplier_id == supplier.id,
            SupplierProduct.id != winner_supplier_product_id,
        )
        .with_for_update(of=ProductBinding)
    ).all()
    duplicate_target_ids: list[int] = []
    for binding, supplier_product, target in rows:
        identity = canonical_supplier_product_identity(
            supplier_code=supplier.code,
            external_id=supplier_product.external_id,
            url=supplier_product.url,
        )
        if identity != external_id.casefold():
            continue
        binding.status = BindingStatus.DISABLED.value
        binding.is_primary = False
        if target is not None:
            target.status = MonitorStatus.DISABLED.value
            duplicate_target_ids.append(target.id)

    if not duplicate_target_ids:
        return
    jobs = db.scalars(
        select(BrowserAgentJob)
        .where(
            BrowserAgentJob.monitor_target_id.in_(duplicate_target_ids),
            BrowserAgentJob.status == BrowserAgentJobStatus.QUEUED.value,
        )
        .with_for_update()
    ).all()
    for job in jobs:
        job.status = BrowserAgentJobStatus.FAILED.value
        job.error_code = "duplicate_supplier_binding"
        job.error_message = (
            "Задание отменено: источник объединён с канонической привязкой"
        )
        job.finished_at = datetime.now(UTC)


def _cancel_stale_browser_jobs(
    db: Session,
    *,
    monitor_target_id: int,
    supplier_product_id: int,
    url: str,
) -> int:
    jobs = db.scalars(
        select(BrowserAgentJob)
        .where(
            BrowserAgentJob.monitor_target_id == monitor_target_id,
            BrowserAgentJob.status.in_(
                (
                    BrowserAgentJobStatus.QUEUED.value,
                    BrowserAgentJobStatus.LEASED.value,
                )
            ),
        )
        .with_for_update()
    ).all()
    cancelled = 0
    finished_at = datetime.now(UTC)
    for job in jobs:
        if job.supplier_product_id == supplier_product_id and job.url == url:
            continue
        job.status = BrowserAgentJobStatus.FAILED.value
        job.error_code = "supplier_source_replaced"
        job.error_message = (
            "Задание отменено: ссылка источника закупки была заменена"
        )
        job.finished_at = finished_at
        job.lease_owner = None
        job.lease_token = None
        job.lease_until = None
        cancelled += 1
    return cancelled


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

    matching_products = _matching_supplier_products(
        db,
        supplier=supplier,
        external_id=external_id,
    )
    supplier_product = matching_products[0][0] if matching_products else None
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
        exact_identity_owner = next(
            (
                item
                for item, _state in matching_products
                if item.external_id == external_id
            ),
            None,
        )
        if exact_identity_owner is None:
            supplier_product.external_id = external_id
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

    _disable_duplicate_bindings(
        db,
        product_id=product.id,
        supplier=supplier,
        external_id=external_id,
        winner_supplier_product_id=supplier_product.id,
    )

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
        queue_result = queue_browser_target_now(
            db,
            target_id=monitor_target.id,
            supplier_code=supplier.code,
        )
        if queue_result.job_id is not None:
            job = db.get(BrowserAgentJob, queue_result.job_id)

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


@router.patch(
    "/products/{product_id}/supplier-bindings/{binding_id}/manual",
    response_model=ManualSupplierBindingUpdateResult,
)
def update_manual_supplier_binding(
    product_id: int,
    binding_id: int,
    payload: ManualSupplierBindingUpdate,
    db: Session = Depends(get_db),
) -> ManualSupplierBindingUpdateResult:
    row = db.execute(
        select(ProductBinding, SupplierProduct, Supplier, MonitorTarget)
        .join(
            SupplierProduct,
            SupplierProduct.id == ProductBinding.supplier_product_id,
        )
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(
            MonitorTarget,
            MonitorTarget.product_binding_id == ProductBinding.id,
        )
        .where(
            ProductBinding.id == binding_id,
            ProductBinding.product_id == product_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Источник закупки не найден")

    binding, previous_supplier_product, previous_supplier, monitor_target = row
    previous_supplier_product_id = previous_supplier_product.id
    previous_url = previous_supplier_product.url
    if previous_supplier.code.startswith(("offline-", "production-")):
        raise HTTPException(
            status_code=422,
            detail="Заменить ссылку можно только у онлайн-поставщика",
        )

    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")

    url = str(payload.url)
    supplier_code, supplier_name, external_id = _source_from_url(url)
    supplier = db.scalar(
        select(Supplier)
        .where(Supplier.code == supplier_code)
        .with_for_update()
    )
    if supplier is None:
        supplier = Supplier(
            code=supplier_code,
            name=supplier_name,
            is_active=True,
        )
        db.add(supplier)
        db.flush()

    matching_products = _matching_supplier_products(
        db,
        supplier=supplier,
        external_id=external_id,
    )
    supplier_product = matching_products[0][0] if matching_products else None
    created_supplier_product = supplier_product is None
    if supplier_product is None:
        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=external_id,
            title=previous_supplier_product.title or product.name,
            url=url,
        )
        db.add(supplier_product)
        db.flush()
    else:
        duplicate_binding_id = db.scalar(
            select(ProductBinding.id).where(
                ProductBinding.product_id == product_id,
                ProductBinding.supplier_product_id == supplier_product.id,
                ProductBinding.id != binding.id,
            )
        )
        if duplicate_binding_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Эта ссылка уже подключена к товару как другой источник "
                    f"(binding {duplicate_binding_id})"
                ),
            )
        exact_identity_owner = next(
            (
                item
                for item, _state in matching_products
                if item.external_id == external_id
            ),
            None,
        )
        if exact_identity_owner is None:
            supplier_product.external_id = external_id
        supplier_product.url = url

    source_changed = (
        previous_supplier_product_id != supplier_product.id
        or previous_url != url
    )
    binding.supplier_product_id = supplier_product.id
    binding.status = BindingStatus.ACTIVE.value
    binding.decision_source = "manual"
    binding.last_validated_at = datetime.now(UTC)

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
        if source_changed:
            monitor_target.last_checked_at = None
            monitor_target.consecutive_failures = 0
            monitor_target.lease_owner = None
            monitor_target.lease_token = None
            monitor_target.lease_until = None

    cancelled_stale_jobs = _cancel_stale_browser_jobs(
        db,
        monitor_target_id=monitor_target.id,
        supplier_product_id=supplier_product.id,
        url=url,
    )

    job: BrowserAgentJob | None = None
    if payload.run_initial_check:
        queue_result = queue_browser_target_now(
            db,
            target_id=monitor_target.id,
            supplier_code=supplier.code,
        )
        if queue_result.job_id is not None:
            job = db.get(BrowserAgentJob, queue_result.job_id)

    db.commit()

    return ManualSupplierBindingUpdateResult(
        product_id=product.id,
        binding_id=binding.id,
        monitor_target_id=monitor_target.id,
        previous_supplier_product_id=previous_supplier_product_id,
        supplier_product_id=supplier_product.id,
        supplier_code=supplier.code,
        url=supplier_product.url,
        created_supplier_product=created_supplier_product,
        cancelled_stale_jobs=cancelled_stale_jobs,
        job_id=None if job is None else job.id,
        queued_initial_check=job is not None,
    )
