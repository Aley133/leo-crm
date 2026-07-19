from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    MarketplaceProvider,
)


def _account(external_account_id: str = "kaspi-shop-1") -> MarketplaceAccount:
    return MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id=external_account_id,
        display_name="Kaspi Shop",
        timezone="Asia/Almaty",
    )


def _order(account_id: int, external_order_id: str = "996801988") -> MarketplaceOrder:
    return MarketplaceOrder(
        marketplace_account_id=account_id,
        external_order_id=external_order_id,
        external_code=external_order_id,
        status=MarketplaceOrderStatus.NEW.value,
        currency="KZT",
        total_amount=Decimal("9999.00"),
        ordered_at=datetime.now(UTC),
    )


def test_same_external_order_is_unique_inside_one_account(db_session) -> None:
    account = _account()
    db_session.add(account)
    db_session.flush()

    db_session.add(_order(account.id))
    db_session.commit()

    db_session.add(_order(account.id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_same_external_order_id_is_allowed_for_different_accounts(db_session) -> None:
    first = _account("kaspi-shop-1")
    second = _account("kaspi-shop-2")
    db_session.add_all([first, second])
    db_session.flush()

    db_session.add_all([_order(first.id), _order(second.id)])
    db_session.commit()

    assert db_session.query(MarketplaceOrder).count() == 2


def test_order_lines_and_events_are_owned_by_order(db_session) -> None:
    account = _account()
    db_session.add(account)
    db_session.flush()

    order = _order(account.id)
    order.lines.append(
        MarketplaceOrderLine(
            external_line_id="line-1",
            external_product_id="102020376",
            merchant_sku="102020376_182073141",
            title="Test product",
            quantity=1,
            unit_price=Decimal("9999.00"),
            line_total=Decimal("9999.00"),
        )
    )
    order.events.append(
        MarketplaceOrderEvent(
            source_event_key="996801988:new",
            event_type="status_changed",
            previous_status=None,
            current_status=MarketplaceOrderStatus.NEW.value,
            occurred_at=datetime.now(UTC),
            metadata_json={"source": "kaspi"},
        )
    )
    db_session.add(order)
    db_session.commit()

    assert len(order.lines) == 1
    assert len(order.events) == 1
    assert order.lines[0].order.id == order.id
    assert order.events[0].order.id == order.id


def test_order_event_source_key_is_idempotent_per_order(db_session) -> None:
    account = _account()
    db_session.add(account)
    db_session.flush()
    order = _order(account.id)
    db_session.add(order)
    db_session.flush()

    event_kwargs = {
        "marketplace_order_id": order.id,
        "source_event_key": "996801988:accepted",
        "event_type": "status_changed",
        "current_status": MarketplaceOrderStatus.ACCEPTED.value,
        "occurred_at": datetime.now(UTC),
    }
    db_session.add(MarketplaceOrderEvent(**event_kwargs))
    db_session.commit()

    db_session.add(MarketplaceOrderEvent(**event_kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
