from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import MarketplaceOrder
from .purchase_api import (
    CreateFromOrderRequest,
    PurchaseResponse,
    TransitionRequest,
    _response,
)
from .purchase_models import PurchaseRequest
from .purchase_service import (
    InvalidPurchaseTransition,
    PurchaseLifecycleError,
    PurchaseVersionConflict,
    create_purchase_from_marketplace_order,
    transition_purchase,
)
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(prefix="/api/workspace/purchases", tags=["workspace-purchases"])


def _owned_order(db: Session, order_id: int, workspace_id: int) -> MarketplaceOrder:
    order = db.get(MarketplaceOrder, order_id)
    if order is None or order.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Marketplace order not found")
    return order


def _owned_purchase(db: Session, purchase_id: UUID, workspace_id: int) -> PurchaseRequest:
    purchase = db.scalar(
        select(PurchaseRequest)
        .join(MarketplaceOrder, MarketplaceOrder.id == PurchaseRequest.marketplace_order_id)
        .where(PurchaseRequest.id == purchase_id, MarketplaceOrder.workspace_id == workspace_id)
    )
    if purchase is None:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    return purchase


@router.post("/from-marketplace-order", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
def create_workspace_purchase(
    payload: CreateFromOrderRequest,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> PurchaseResponse:
    try:
        with db.begin():
            _owned_order(db, payload.marketplace_order_id, principal.workspace_id)
            purchase = create_purchase_from_marketplace_order(
                db,
                marketplace_order_id=payload.marketplace_order_id,
                idempotency_key=payload.idempotency_key,
                note=payload.note,
            )
        return _response(purchase)
    except PurchaseLifecycleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{purchase_request_id}/transition", response_model=PurchaseResponse)
def transition_workspace_purchase(
    purchase_request_id: UUID,
    payload: TransitionRequest,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> PurchaseResponse:
    try:
        with db.begin():
            _owned_purchase(db, purchase_request_id, principal.workspace_id)
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidPurchaseTransition as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PurchaseLifecycleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
