from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import observation_engine
from . import fast_dumping_service as svc
from .dumping_service import physical_stock_count
from .fast_dumping_models import FastDumpingPolicy, FastDumpingState
from .models import Product
from .suppliers import ProductBinding


_INSTALLED = False
_PREVIOUS_PERSIST_SUCCESS = None


def _wake_fast_products_for_supplier(
    db: Session,
    *,
    supplier_product_id: int,
    changed: bool,
) -> int:
    """Wake Fast-owned offers after an accepted supplier observation.

    Supplier monitoring is authoritative for preorder price/delivery/availability.
    We only need an immediate Fast rescan while physical FIFO is already zero;
    with warehouse stock on hand the supplier is intentionally dormant until the
    inventory transition itself wakes Fast Dumping.
    """
    if not changed:
        return 0

    bound_products = db.scalars(
        select(Product)
        .join(ProductBinding, ProductBinding.product_id == Product.id)
        .where(
            ProductBinding.supplier_product_id == supplier_product_id,
            ProductBinding.status.in_(("active", "confirmed", "degraded")),
        )
    ).all()
    if not bound_products:
        return 0

    owner_ids = {
        int(product.inventory_owner_product_id or product.id)
        for product in bound_products
    }
    candidates = db.execute(
        select(FastDumpingPolicy, Product, FastDumpingState)
        .join(Product, Product.id == FastDumpingPolicy.product_id)
        .outerjoin(
            FastDumpingState,
            (FastDumpingState.workspace_id == FastDumpingPolicy.workspace_id)
            & (FastDumpingState.product_id == FastDumpingPolicy.product_id),
        )
        .where(
            FastDumpingPolicy.enabled.is_(True),
            or_(
                Product.id.in_(owner_ids),
                Product.inventory_owner_product_id.in_(owner_ids),
            ),
        )
    ).all()

    awakened = 0
    now = svc.utcnow()
    for policy, product, state in candidates:
        if physical_stock_count(db, product_id=product.id) > 0:
            continue
        if state is None:
            state = svc.ensure_state(
                db,
                policy=policy,
                workspace_id=policy.workspace_id,
            )
        state.next_scan_at = now
        state.status_reason = (
            "Поставщик обновил цену/доступность/срок доставки. "
            "Fast Dumping немедленно пересчитывает realtime preorder/off-state."
        )
        if state.active_job_id is None and not state.automatic_writes_paused:
            _job, queued = svc.queue_scan(
                db,
                policy=policy,
                workspace_id=policy.workspace_id,
                reason="supplier_offer_changed",
            )
            awakened += int(bool(queued))
        else:
            awakened += 1
    return awakened


def _persist_successful_observation(db: Session, **kwargs: Any):
    result = _PREVIOUS_PERSIST_SUCCESS(db, **kwargs)
    _wake_fast_products_for_supplier(
        db,
        supplier_product_id=int(result.supplier_product_id),
        changed=bool(result.changed),
    )
    return result


def install_fast_dumping_supplier_events() -> None:
    global _INSTALLED, _PREVIOUS_PERSIST_SUCCESS
    if _INSTALLED:
        return
    _INSTALLED = True
    _PREVIOUS_PERSIST_SUCCESS = observation_engine.persist_successful_observation
    observation_engine.persist_successful_observation = _persist_successful_observation
