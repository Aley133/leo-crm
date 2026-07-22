from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .purchase_service import (
    InvalidPurchaseTransition,
    PurchaseLifecycleError,
    PurchaseVersionConflict,
    create_purchase_from_marketplace_order,
    transition_purchase,
)


router = APIRouter(
    prefix="/api/purchases",
    tags=["purchases"],
    dependencies=[Depends(require_service_token)],
)


class CreateFromOrderRequest(BaseModel):
    marketplace_order_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=2000)


class TransitionRequest(BaseModel):
    target_status: str
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=255)
    metadata: dict | None = None


class PurchaseResponse(BaseModel):
    id: UUID
    marketplace_order_id: int | None
    origin: str
    status: str
    currency: str
    expected_total: float | None
    version: int
    line_count: int
    first_product_id: int | None = None


def _response(purchase) -> PurchaseResponse:
    first_product_id = next(
        (line.product_id for line in purchase.lines if line.product_id is not None),
        None,
    )
    return PurchaseResponse(
        id=purchase.id,
        marketplace_order_id=purchase.marketplace_order_id,
        origin=purchase.origin,
        status=purchase.status,
        currency=purchase.currency,
        expected_total=float(purchase.expected_total) if purchase.expected_total is not None else None,
        version=purchase.version,
        line_count=len(purchase.lines),
        first_product_id=first_product_id,
    )


@router.post("/from-marketplace-order", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_from_marketplace_order(
    payload: CreateFromOrderRequest,
    db: Session = Depends(get_db),
) -> PurchaseResponse:
    try:
        with db.begin():
            purchase = create_purchase_from_marketplace_order(
                db,
                marketplace_order_id=payload.marketplace_order_id,
                idempotency_key=payload.idempotency_key,
                note=payload.note,
            )
        return _response(purchase)
    except PurchaseLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{purchase_request_id}/transition", response_model=PurchaseResponse)
def transition(
    purchase_request_id: UUID,
    payload: TransitionRequest,
    db: Session = Depends(get_db),
) -> PurchaseResponse:
    try:
        with db.begin():
            purchase = transition_purchase(
                db,
                purchase_request_id=purchase_request_id,
                target_status=payload.target_status,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                metadata=payload.metadata,
            )
        return _response(purchase)
    except PurchaseVersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidPurchaseTransition as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except PurchaseLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
