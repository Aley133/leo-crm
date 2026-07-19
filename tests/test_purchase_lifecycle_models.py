from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceProvider
from backend.app.purchase_models import (
    PurchaseEvent,
    PurchaseOrigin,
    PurchaseReceipt,
    PurchaseReceiptLine,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseStatus,
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


def test_manual_purchase_request_is_valid_without_marketplace_order(session_factory) -> None:
    with session_factory() as session:
        with session.begin():
            request = PurchaseRequest(origin=PurchaseOrigin.MANUAL.value)
            request.lines.append(PurchaseRequestLine(title="Manual item", quantity=2))
            session.add(request)

        stored = session.get(PurchaseRequest, request.id)
        assert stored is not None
        assert stored.marketplace_order_id is None
        assert stored.status == PurchaseStatus.DRAFT.value
        assert stored.version == 1
        assert stored.lines[0].received_quantity == 0


def test_purchase_request_can_reference_normalized_marketplace_order(session_factory) -> None:
    with session_factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider=MarketplaceProvider.KASPI.value,
                external_account_id="merchant-purchase-test",
                display_name="Kaspi Shop",
                timezone="Asia/Almaty",
            )
            session.add(account)
            session.flush()
            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id="order-purchase-test",
                status="new",
                original_status="NEW",
                currency="KZT",
                total_amount=15000,
                version=1,
            )
            session.add(order)
            session.flush()
            request = PurchaseRequest(
                marketplace_order_id=order.id,
                origin=PurchaseOrigin.MARKETPLACE_ORDER.value,
            )
            session.add(request)

        assert request.marketplace_order_id == order.id


def test_purchase_line_rejects_invalid_received_quantity(session_factory) -> None:
    with session_factory() as session:
        request = PurchaseRequest(origin=PurchaseOrigin.MANUAL.value)
        request.lines.append(
            PurchaseRequestLine(
                title="Invalid item",
                quantity=1,
                received_quantity=2,
            )
        )
        session.add(request)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_purchase_event_idempotency_is_scoped_to_request(session_factory) -> None:
    occurred_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        with session.begin():
            first = PurchaseRequest(origin=PurchaseOrigin.MANUAL.value)
            second = PurchaseRequest(origin=PurchaseOrigin.MANUAL.value)
            session.add_all([first, second])
            session.flush()
            session.add_all(
                [
                    PurchaseEvent(
                        purchase_request_id=first.id,
                        idempotency_key="request-created",
                        event_type="purchase.created",
                        current_status=PurchaseStatus.DRAFT.value,
                        occurred_at=occurred_at,
                    ),
                    PurchaseEvent(
                        purchase_request_id=second.id,
                        idempotency_key="request-created",
                        event_type="purchase.created",
                        current_status=PurchaseStatus.DRAFT.value,
                        occurred_at=occurred_at,
                    ),
                ]
            )

        assert len(session.scalars(select(PurchaseEvent)).all()) == 2

        session.add(
            PurchaseEvent(
                purchase_request_id=first.id,
                idempotency_key="request-created",
                event_type="purchase.created",
                current_status=PurchaseStatus.DRAFT.value,
                occurred_at=occurred_at,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_receipt_line_identity_and_positive_quantity(session_factory) -> None:
    received_at = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)
    with session_factory() as session:
        with session.begin():
            request = PurchaseRequest(origin=PurchaseOrigin.MANUAL.value)
            line = PurchaseRequestLine(title="Received item", quantity=3)
            request.lines.append(line)
            session.add(request)
            session.flush()
            receipt = PurchaseReceipt(
                purchase_request_id=request.id,
                received_at=received_at,
            )
            session.add(receipt)
            session.flush()
            session.add(
                PurchaseReceiptLine(
                    purchase_receipt_id=receipt.id,
                    purchase_request_line_id=line.id,
                    quantity=1,
                )
            )

        duplicate = PurchaseReceiptLine(
            purchase_receipt_id=receipt.id,
            purchase_request_line_id=line.id,
            quantity=1,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
