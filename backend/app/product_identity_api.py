from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select

from .auth import require_service_token
from .db import SessionLocal
from .product_identity_models import MarketplaceListing, MarketplaceListingEvent
from .product_resolver import resolve_listing, unresolve_listing


router = APIRouter(
    prefix="/api/marketplace-listings",
    tags=["marketplace-listings"],
    dependencies=[Depends(require_service_token)],
)


class ResolveListingRequest(BaseModel):
    product_id: int = Field(gt=0)
    actor: str | None = Field(default=None, max_length=255)


class UnresolveListingRequest(BaseModel):
    actor: str | None = Field(default=None, max_length=255)


def _payload(listing: MarketplaceListing) -> dict[str, object]:
    return {
        "id": listing.id,
        "marketplace_account_id": listing.marketplace_account_id,
        "identity_kind": listing.identity_kind,
        "identity_key": listing.identity_key,
        "merchant_sku": listing.merchant_sku,
        "external_product_id": listing.external_product_id,
        "product_id": listing.product_id,
        "status": listing.status,
        "resolved_at": listing.resolved_at,
        "created_at": listing.created_at,
        "updated_at": listing.updated_at,
    }


@router.get("")
def list_marketplace_listings(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    listing_status: str | None = Query(default="unresolved", alias="status"),
    query: str | None = Query(default=None, min_length=1, max_length=200),
) -> dict[str, object]:
    filters = []
    if listing_status:
        filters.append(MarketplaceListing.status == listing_status)
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(
            or_(
                MarketplaceListing.identity_key.ilike(pattern),
                MarketplaceListing.merchant_sku.ilike(pattern),
                MarketplaceListing.external_product_id.ilike(pattern),
            )
        )

    with SessionLocal() as session:
        total = session.scalar(select(func.count(MarketplaceListing.id)).where(*filters)) or 0
        listings = session.scalars(
            select(MarketplaceListing)
            .where(*filters)
            .order_by(MarketplaceListing.created_at.desc(), MarketplaceListing.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_payload(listing) for listing in listings],
    }


@router.get("/{listing_id}")
def get_marketplace_listing(listing_id: int) -> dict[str, object]:
    with SessionLocal() as session:
        listing = session.get(MarketplaceListing, listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Marketplace listing not found")
        events = session.scalars(
            select(MarketplaceListingEvent)
            .where(MarketplaceListingEvent.marketplace_listing_id == listing_id)
            .order_by(MarketplaceListingEvent.occurred_at, MarketplaceListingEvent.id)
        ).all()
        payload = _payload(listing)
        payload["events"] = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "previous_product_id": event.previous_product_id,
                "current_product_id": event.current_product_id,
                "metadata": event.metadata_json,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ]
        return payload


@router.post("/{listing_id}/resolve")
def resolve_marketplace_listing(
    listing_id: int,
    payload: ResolveListingRequest,
) -> dict[str, object]:
    with SessionLocal() as session:
        try:
            with session.begin():
                listing = resolve_listing(
                    session,
                    listing_id=listing_id,
                    product_id=payload.product_id,
                    actor=payload.actor,
                )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        session.refresh(listing)
        return _payload(listing)


@router.post("/{listing_id}/unresolve")
def unresolve_marketplace_listing(
    listing_id: int,
    payload: UnresolveListingRequest,
) -> dict[str, object]:
    with SessionLocal() as session:
        try:
            with session.begin():
                listing = unresolve_listing(
                    session,
                    listing_id=listing_id,
                    actor=payload.actor,
                )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        session.refresh(listing)
        return _payload(listing)
