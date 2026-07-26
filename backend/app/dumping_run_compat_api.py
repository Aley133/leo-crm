from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .dumping_competitor_worker import enqueue_competitor_scan
from .dumping_models import DumpingPolicy
from .models import Product

router = APIRouter(
    prefix="/api/dumping",
    tags=["dumping"],
    dependencies=[Depends(require_service_token)],
)


@router.post("/products/{product_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
def queue_dumping_run_now(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    policy = db.scalar(select(DumpingPolicy).where(DumpingPolicy.product_id == product_id))
    if policy is None or not policy.enabled:
        raise HTTPException(status_code=409, detail="Демпинг для товара не подключён")

    try:
        queued = enqueue_competitor_scan(product_id, reason="manual")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "status": "queued" if queued else "already_queued",
        "queued": queued,
        "product_id": product_id,
        "message": (
            "Карточка поставлена в очередь локального Kaspi Competitor Agent"
            if queued
            else "Карточка уже ожидает локальную проверку"
        ),
    }
