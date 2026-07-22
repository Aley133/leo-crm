from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.marketplace_sync import sync_kaspi_order_page
from backend.app.marketplace_transport import MarketplaceOrderPage
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceProvider


class DetailedTransport:
    def __init__(self) -> None:
        self.detail_calls: list[str] = []

    def fetch_orders(
        self,
        *,
        cursor: str | None,
        updated_after: datetime | None,
        limit: int,
    ) -> MarketplaceOrderPage:
        return MarketplaceOrderPage(
            items=(
                {
                    "id": "order-1",
                    "attributes": {
                        "code": "1006480798",
                        "status": "ACCEPTED_BY_MERCHANT",
                        "state": "KASPI_DELIVERY",
                        "currency": "KZT",
                        "totalPrice": "7700",
                        "entries": [],
                    },
                },
            ),
            next_cursor=None,
        )

    def fetch_order_by_code(self, order_code: str) -> dict:
        self.detail_calls.append(order_code)
        return {
            "id": "order-1",
            "attributes": {
                "code": order_code,
                "status": "ACCEPTED_BY_MERCHANT",
                "state": "KASPI_DELIVERY",
                "preOrder": False,
                "assembled": True,
                "kaspiDelivery": {"courierTransmissionDate": None},
                "currency": "KZT",
                "totalPrice": "7700",
                "entries": [],
            },
        }


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


def _account_id(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider=MarketplaceProvider.KASPI.value,
                external_account_id="merchant-1",
                display_name="Kaspi Shop",
                timezone="Asia/Almaty",
            )
            session.add(account)
            session.flush()
            return account.id


def test_accepted_list_payload_is_replaced_by_detailed_raw_order_before_import(
    session_factory,
) -> None:
    account_id = _account_id(session_factory)
    transport = DetailedTransport()

    sync_kaspi_order_page(
        session_factory,
        transport,
        marketplace_account_id=account_id,
        limit=10,
    )

    assert transport.detail_calls == ["1006480798"]
    with session_factory() as session:
        order = session.scalar(select(MarketplaceOrder))
        assert order is not None
        # In the archived raw-board model deliveryCostForSeller <= 0 and
        # preOrder=false means the order is still in the packing column. The old
        # Browser Agent `assembled=true` flag is deliberately not authoritative.
        assert order.status == "assembly"
        assert order.original_status == "ASSEMBLY"
