from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from backend.app.marketplace_import import import_kaspi_order, normalize_kaspi_order
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceImportCheckpoint,
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderStatus,
    MarketplaceProvider,
    MarketplaceRawPayload,
)


def _account(db_session) -> MarketplaceAccount:
    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-1",
        display_name="Kaspi test account",
    )
    db_session.add(account)
    db_session.flush()
    return account


def _payload(*, status: str = "NEW", revision: str = "1", price: int = 1000) -> dict:
    return {
        "id": "order-100",
        "attributes": {
            "code": "996801988",
            "status": status,
            "revision": revision,
            "currency": "KZT",
            "totalPrice": price,
            "creationDate": "2026-07-19T10:00:00Z",
            "updatedAt": "2026-07-19T10:05:00Z",
            "entries": [
                {
                    "id": "line-1",
                    "attributes": {
                        "offerCode": "SKU-1",
                        "productId": "kaspi-product-1",
                        "name": "Test product",
                        "quantity": 1,
                        "basePrice": price,
                        "totalPrice": price,
                    },
                }
            ],
        },
    }


def test_unknown_kaspi_status_is_retained_and_normalized_to_unknown() -> None:
    normalized = normalize_kaspi_order(_payload(status="SOME_NEW_KASPI_STATE"))

    assert normalized.status == MarketplaceOrderStatus.UNKNOWN.value
    assert normalized.original_status == "SOME_NEW_KASPI_STATE"


def test_kaspi_lifecycle_status_wins_over_fulfilment_channel() -> None:
    payload = _payload(status="ACCEPTED_BY_MERCHANT")
    payload["attributes"]["state"] = "KASPI_DELIVERY"

    normalized = normalize_kaspi_order(payload)

    assert normalized.status == MarketplaceOrderStatus.ACCEPTED.value
    assert normalized.original_status == "ACCEPTED_BY_MERCHANT"


def test_duplicate_payload_is_idempotent(db_session) -> None:
    account = _account(db_session)
    payload = _payload()

    first = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=payload,
        checkpoint_cursor="cursor-1",
    )
    db_session.commit()

    second = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=payload,
        checkpoint_cursor="cursor-1",
    )
    db_session.commit()

    assert first.created is True
    assert first.changed is True
    assert first.raw_payload_created is True
    assert second.created is False
    assert second.changed is False
    assert second.raw_payload_created is False
    assert db_session.scalar(select(MarketplaceOrder).where(MarketplaceOrder.id == first.order_id))
    assert len(db_session.scalars(select(MarketplaceRawPayload)).all()) == 1
    assert len(db_session.scalars(select(MarketplaceOrderEvent)).all()) == 1


def test_changed_status_updates_order_and_appends_event(db_session) -> None:
    account = _account(db_session)

    first = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(status="NEW", revision="1"),
    )
    db_session.commit()

    second = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(status="DELIVERED", revision="2"),
    )
    db_session.commit()

    order = db_session.get(MarketplaceOrder, first.order_id)
    events = db_session.scalars(
        select(MarketplaceOrderEvent)
        .where(MarketplaceOrderEvent.marketplace_order_id == first.order_id)
        .order_by(MarketplaceOrderEvent.id)
    ).all()

    assert second.created is False
    assert second.changed is True
    assert order is not None
    assert order.status == MarketplaceOrderStatus.DELIVERED.value
    assert order.original_status == "DELIVERED"
    assert order.source_revision == "2"
    assert order.version == 2
    assert len(events) == 2
    assert events[-1].previous_status == MarketplaceOrderStatus.NEW.value
    assert events[-1].current_status == MarketplaceOrderStatus.DELIVERED.value


def test_order_lines_are_updated_without_duplicate_rows(db_session) -> None:
    account = _account(db_session)

    result = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(price=1000, revision="1"),
    )
    db_session.commit()

    import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(price=1200, revision="2"),
    )
    db_session.commit()

    order = db_session.get(MarketplaceOrder, result.order_id)
    assert order is not None
    assert len(order.lines) == 1
    assert Decimal(order.lines[0].unit_price) == Decimal("1200.00")
    assert Decimal(order.total_amount) == Decimal("1200.00")


def test_rollback_keeps_checkpoint_and_order_unchanged(db_session) -> None:
    account = _account(db_session)
    initial_watermark = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    first = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(status="NEW", revision="1"),
        checkpoint_cursor="cursor-1",
        checkpoint_watermark_at=initial_watermark,
    )
    db_session.commit()

    import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(status="DELIVERED", revision="2"),
        checkpoint_cursor="cursor-2",
        checkpoint_watermark_at=datetime(2026, 7, 19, 11, 0, tzinfo=UTC),
    )
    db_session.rollback()

    db_session.expire_all()
    order = db_session.get(MarketplaceOrder, first.order_id)
    checkpoint = db_session.scalar(
        select(MarketplaceImportCheckpoint).where(
            MarketplaceImportCheckpoint.marketplace_account_id == account.id,
            MarketplaceImportCheckpoint.stream_name == "orders",
        )
    )

    assert order is not None
    assert order.status == MarketplaceOrderStatus.NEW.value
    assert order.version == 1
    assert checkpoint is not None
    assert checkpoint.cursor == "cursor-1"
    assert checkpoint.watermark_at == initial_watermark
