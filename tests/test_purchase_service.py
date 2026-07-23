from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, OutboxEvent
from backend.app.purchase_models import PurchaseEvent, PurchaseRequest, PurchaseStatus
from backend.app.purchase_service import (
    InvalidPurchaseTransition,
    PurchaseVersionConflict,
    create_purchase_from_marketplace_order,
    transition_purchase,
)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _order_id(factory) -> int:
    with factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider="kaspi",
                external_account_id="merchant-live",
                display_name="Kaspi Live",
                timezone="Asia/Almaty",
            )
            session.add(account)
            session.flush()
            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id="order-live-1",
                external_code="996801988",
                status="new",
                original_status="NEW",
                currency="KZT",
                total_amount=15000,
                ordered_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
                version=1,
            )
            order.lines.append(
                MarketplaceOrderLine(
                    external_line_id="line-1",
                    merchant_sku="SKU-1",
                    title="Real product",
                    quantity=2,
                    unit_price=7500,
                    line_total=15000,
                )
            )
            session.add(order)
            session.flush()
            return order.id


def test_create_purchase_from_order_is_idempotent(session_factory) -> None:
    order_id = _order_id(session_factory)
    with session_factory() as session:
        with session.begin():
            first = create_purchase_from_marketplace_order(
                session,
                marketplace_order_id=order_id,
                idempotency_key="purchase:create:order-live-1",
            )
            first_id = first.id

    with session_factory() as session:
        with session.begin():
            second = create_purchase_from_marketplace_order(
                session,
                marketplace_order_id=order_id,
                idempotency_key="purchase:create:order-live-1",
            )
            assert second.id == first_id

    with session_factory() as session:
        assert len(session.scalars(select(PurchaseRequest)).all()) == 1
        assert len(session.scalars(select(PurchaseEvent)).all()) == 1
        assert len(session.scalars(select(OutboxEvent)).all()) == 1


def test_transition_checks_version_and_emits_once(session_factory) -> None:
    order_id = _order_id(session_factory)
    with session_factory() as session:
        with session.begin():
            purchase = create_purchase_from_marketplace_order(
                session,
                marketplace_order_id=order_id,
                idempotency_key="create-1",
            )
            purchase_id = purchase.id

    with session_factory() as session:
        with session.begin():
            purchase = transition_purchase(
                session,
                purchase_request_id=purchase_id,
                target_status=PurchaseStatus.REQUESTED.value,
                expected_version=1,
                idempotency_key="transition-requested-1",
            )
            assert purchase.status == PurchaseStatus.REQUESTED.value
            assert purchase.version == 2

    with session_factory() as session:
        with session.begin():
            repeated = transition_purchase(
                session,
                purchase_request_id=purchase_id,
                target_status=PurchaseStatus.REQUESTED.value,
                expected_version=1,
                idempotency_key="transition-requested-1",
            )
            assert repeated.version == 2

    with session_factory() as session:
        with pytest.raises(PurchaseVersionConflict):
            with session.begin():
                transition_purchase(
                    session,
                    purchase_request_id=purchase_id,
                    target_status=PurchaseStatus.ORDERED.value,
                    expected_version=1,
                    idempotency_key="stale-command",
                )

    with session_factory() as session:
        assert len(session.scalars(select(PurchaseEvent)).all()) == 2
        assert len(session.scalars(select(OutboxEvent)).all()) == 2


def test_invalid_transition_and_incomplete_close_are_rejected(session_factory) -> None:
    order_id = _order_id(session_factory)
    with session_factory() as session:
        with session.begin():
            purchase = create_purchase_from_marketplace_order(
                session,
                marketplace_order_id=order_id,
                idempotency_key="create-2",
            )
            purchase_id = purchase.id

    with session_factory() as session:
        with pytest.raises(InvalidPurchaseTransition):
            with session.begin():
                transition_purchase(
                    session,
                    purchase_request_id=purchase_id,
                    target_status=PurchaseStatus.CLOSED.value,
                    expected_version=1,
                    idempotency_key="invalid-close",
                )


def test_mark_received_fills_quantities_and_allows_close(session_factory) -> None:
    order_id = _order_id(session_factory)
    with session_factory() as session:
        with session.begin():
            purchase = create_purchase_from_marketplace_order(
                session,
                marketplace_order_id=order_id,
                idempotency_key="create-receipt",
            )
            purchase_id = purchase.id

    transitions = (
        (PurchaseStatus.REQUESTED.value, 1),
        (PurchaseStatus.ORDERED.value, 2),
        (PurchaseStatus.RECEIVED.value, 3),
    )
    for target, version in transitions:
        with session_factory() as session:
            with session.begin():
                purchase = transition_purchase(
                    session,
                    purchase_request_id=purchase_id,
                    target_status=target,
                    expected_version=version,
                    idempotency_key=f"receive-flow:{target}",
                )

    assert purchase.status == PurchaseStatus.RECEIVED.value
    assert all(line.received_quantity == line.quantity for line in purchase.lines)

    with session_factory() as session:
        with session.begin():
            closed = transition_purchase(
                session,
                purchase_request_id=purchase_id,
                target_status=PurchaseStatus.CLOSED.value,
                expected_version=4,
                idempotency_key="receive-flow:closed",
            )
            assert closed.status == PurchaseStatus.CLOSED.value
