from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .kaspi_order_line_display import recover_order_line_title
from .models import MarketplaceOrder, MarketplaceRawPayload, OutboxEvent, Product
from .purchase_models import (
    PurchaseEvent,
    PurchaseOrigin,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseStatus,
)


class PurchaseLifecycleError(RuntimeError):
    pass


class PurchaseVersionConflict(PurchaseLifecycleError):
    pass


class InvalidPurchaseTransition(PurchaseLifecycleError):
    pass


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PurchaseStatus.DRAFT.value: {
        PurchaseStatus.REQUESTED.value,
        PurchaseStatus.CANCELLED.value,
    },
    PurchaseStatus.REQUESTED.value: {
        PurchaseStatus.ORDERED.value,
        PurchaseStatus.CANCELLED.value,
    },
    PurchaseStatus.ORDERED.value: {
        PurchaseStatus.PARTIALLY_RECEIVED.value,
        PurchaseStatus.RECEIVED.value,
        PurchaseStatus.CANCELLED.value,
    },
    PurchaseStatus.PARTIALLY_RECEIVED.value: {
        PurchaseStatus.RECEIVED.value,
        PurchaseStatus.CANCELLED.value,
    },
    PurchaseStatus.RECEIVED.value: {PurchaseStatus.CLOSED.value},
    PurchaseStatus.CANCELLED.value: set(),
    PurchaseStatus.CLOSED.value: set(),
}


def _latest_order_payload(session: Session, order: MarketplaceOrder) -> dict | None:
    return session.scalar(
        select(MarketplaceRawPayload.payload_json)
        .where(
            MarketplaceRawPayload.marketplace_account_id == order.marketplace_account_id,
            MarketplaceRawPayload.payload_type == "order",
            MarketplaceRawPayload.external_object_id == order.external_order_id,
        )
        .order_by(
            MarketplaceRawPayload.received_at.desc(),
            MarketplaceRawPayload.id.desc(),
        )
        .limit(1)
    )


def _ensure_product_card_for_line(
    session: Session,
    *,
    order: MarketplaceOrder,
    order_line,
    raw_payload: dict | None,
) -> int | None:
    if order_line.product_id is not None:
        return order_line.product_id

    kaspi_product_id = (
        (order_line.external_product_id or "").strip()
        or (order_line.merchant_sku or "").strip()
    )
    if not kaspi_product_id:
        return None

    title = (order_line.title or "").strip()
    if not title or title.lower() == "unknown product":
        recovered = recover_order_line_title(
            raw_payload,
            identities=(
                order_line.external_line_id,
                order_line.external_product_id,
                order_line.merchant_sku,
            ),
        )
        if recovered:
            title = recovered
            order_line.title = recovered

    if not title or title.lower() == "unknown product":
        title = f"Товар Kaspi {kaspi_product_id}"
        order_line.title = title

    product = session.scalar(
        select(Product).where(Product.kaspi_product_id == kaspi_product_id)
    )
    if product is None:
        product = Product(
            kaspi_product_id=kaspi_product_id,
            merchant_sku=order_line.merchant_sku,
            name=title,
        )
        session.add(product)
        session.flush()
    else:
        if product.name.strip().lower() == "unknown product" and title:
            product.name = title
        if product.merchant_sku is None and order_line.merchant_sku:
            product.merchant_sku = order_line.merchant_sku

    order_line.product_id = product.id
    return product.id


def create_purchase_from_marketplace_order(
    session: Session,
    *,
    marketplace_order_id: int,
    idempotency_key: str,
    note: str | None = None,
) -> PurchaseRequest:
    """Create one draft purchase from a marketplace order in caller transaction."""
    existing_event = session.scalar(
        select(PurchaseEvent).where(PurchaseEvent.idempotency_key == idempotency_key)
    )
    if existing_event is not None:
        return session.get(PurchaseRequest, existing_event.purchase_request_id)

    order = session.get(MarketplaceOrder, marketplace_order_id)
    if order is None:
        raise PurchaseLifecycleError("Marketplace order not found")

    existing_purchase = session.scalar(
        select(PurchaseRequest).where(
            PurchaseRequest.marketplace_order_id == marketplace_order_id,
            PurchaseRequest.status != PurchaseStatus.CANCELLED.value,
        )
    )
    if existing_purchase is not None:
        return existing_purchase

    raw_payload = _latest_order_payload(session, order)
    purchase = PurchaseRequest(
        marketplace_order_id=order.id,
        origin=PurchaseOrigin.MARKETPLACE_ORDER.value,
        status=PurchaseStatus.DRAFT.value,
        currency=order.currency,
        expected_total=order.total_amount,
        note=note,
        version=1,
    )
    for order_line in order.lines:
        product_id = _ensure_product_card_for_line(
            session,
            order=order,
            order_line=order_line,
            raw_payload=raw_payload,
        )
        purchase.lines.append(
            PurchaseRequestLine(
                marketplace_order_line_id=order_line.id,
                product_id=product_id,
                title=order_line.title,
                quantity=order_line.quantity,
                received_quantity=0,
                expected_unit_cost=None,
            )
        )
    session.add(purchase)
    session.flush()

    event = PurchaseEvent(
        purchase_request_id=purchase.id,
        idempotency_key=idempotency_key,
        event_type="purchase_created",
        previous_status=None,
        current_status=purchase.status,
        metadata_json={"marketplace_order_id": order.id},
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    session.add(
        OutboxEvent(
            aggregate_type="purchase_request",
            aggregate_id=str(purchase.id),
            event_type="purchase.created",
            idempotency_key=f"purchase.created:{purchase.id}:v1",
            payload_json={
                "purchase_request_id": str(purchase.id),
                "marketplace_order_id": order.id,
                "status": purchase.status,
                "version": purchase.version,
            },
        )
    )
    session.flush()
    return purchase


def transition_purchase(
    session: Session,
    *,
    purchase_request_id: UUID,
    target_status: str,
    expected_version: int,
    idempotency_key: str,
    metadata: dict | None = None,
) -> PurchaseRequest:
    """Apply one validated status transition in the caller-owned transaction."""
    purchase = session.scalar(
        select(PurchaseRequest)
        .where(PurchaseRequest.id == purchase_request_id)
        .with_for_update()
    )
    if purchase is None:
        raise PurchaseLifecycleError("Purchase request not found")

    existing_event = session.scalar(
        select(PurchaseEvent).where(
            PurchaseEvent.purchase_request_id == purchase.id,
            PurchaseEvent.idempotency_key == idempotency_key,
        )
    )
    if existing_event is not None:
        return purchase

    if purchase.version != expected_version:
        raise PurchaseVersionConflict(
            f"Expected version {expected_version}, current version is {purchase.version}"
        )

    allowed = _ALLOWED_TRANSITIONS.get(purchase.status, set())
    if target_status not in allowed:
        raise InvalidPurchaseTransition(
            f"Transition {purchase.status} -> {target_status} is not allowed"
        )

    if target_status == PurchaseStatus.CLOSED.value:
        incomplete = any(line.received_quantity != line.quantity for line in purchase.lines)
        if incomplete:
            raise InvalidPurchaseTransition("Cannot close purchase with unreceived quantity")

    previous_status = purchase.status
    purchase.status = target_status
    purchase.version += 1

    session.add(
        PurchaseEvent(
            purchase_request_id=purchase.id,
            idempotency_key=idempotency_key,
            event_type="status_changed",
            previous_status=previous_status,
            current_status=target_status,
            metadata_json=metadata,
            occurred_at=datetime.now(UTC),
        )
    )
    session.add(
        OutboxEvent(
            aggregate_type="purchase_request",
            aggregate_id=str(purchase.id),
            event_type="purchase.status_changed",
            idempotency_key=f"purchase.status_changed:{purchase.id}:v{purchase.version}",
            payload_json={
                "purchase_request_id": str(purchase.id),
                "previous_status": previous_status,
                "status": purchase.status,
                "version": purchase.version,
            },
        )
    )
    session.flush()
    return purchase
