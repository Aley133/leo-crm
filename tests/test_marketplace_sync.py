from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.marketplace_sync import sync_kaspi_order_page
from backend.app.marketplace_transport import MarketplaceOrderPage
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceImportCheckpoint,
    MarketplaceImportExecution,
    MarketplaceImportStatus,
    MarketplaceOrder,
    MarketplaceProvider,
)


class FakeTransport:
    def __init__(self, page: MarketplaceOrderPage) -> None:
        self.page = page
        self.calls: list[tuple[str | None, datetime | None, int]] = []

    def fetch_orders(
        self,
        *,
        cursor: str | None,
        updated_after: datetime | None,
        limit: int,
    ) -> MarketplaceOrderPage:
        self.calls.append((cursor, updated_after, limit))
        return self.page


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


def _payload(*, status: str = "NEW", order_id: str = "order-1") -> dict:
    return {
        "id": order_id,
        "attributes": {
            "code": "996801988",
            "status": status,
            "revision": f"rev-{status}",
            "currency": "KZT",
            "totalPrice": "15000.00",
            "creationDate": "2026-07-19T10:00:00Z",
            "updatedAt": "2026-07-19T10:05:00Z",
            "entries": [
                {
                    "id": "line-1",
                    "attributes": {
                        "offerCode": "SKU-1",
                        "name": "Test product",
                        "quantity": 1,
                        "basePrice": "15000.00",
                        "totalPrice": "15000.00",
                    },
                }
            ],
        },
    }


def test_sync_commits_orders_execution_and_checkpoint_together(session_factory) -> None:
    account_id = _account_id(session_factory)
    watermark = datetime(2026, 7, 19, 10, 5, tzinfo=UTC)
    transport = FakeTransport(
        MarketplaceOrderPage(
            items=(_payload(),),
            next_cursor="cursor-2",
            watermark_at=watermark,
        )
    )

    result = sync_kaspi_order_page(
        session_factory,
        transport,
        marketplace_account_id=account_id,
        limit=50,
    )

    assert result.fetched_count == 1
    assert result.imported_count == 1
    assert result.updated_count == 0
    assert transport.calls == [(None, None, 50)]

    with session_factory() as session:
        order = session.scalar(select(MarketplaceOrder))
        checkpoint = session.scalar(select(MarketplaceImportCheckpoint))
        execution = session.get(MarketplaceImportExecution, result.execution_id)
        assert order is not None
        assert checkpoint is not None
        assert checkpoint.cursor == "cursor-2"
        assert checkpoint.watermark_at == watermark
        assert execution is not None
        assert execution.status == MarketplaceImportStatus.SUCCEEDED.value
        assert execution.imported_count == 1


def test_failed_page_persistence_does_not_advance_checkpoint(session_factory) -> None:
    account_id = _account_id(session_factory)
    old_watermark = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    with session_factory() as session:
        with session.begin():
            session.add(
                MarketplaceImportCheckpoint(
                    marketplace_account_id=account_id,
                    stream_name="orders",
                    cursor="cursor-old",
                    watermark_at=old_watermark,
                )
            )

    invalid_payload = _payload(order_id="")
    invalid_payload["id"] = ""
    invalid_payload["attributes"].pop("code", None)
    transport = FakeTransport(
        MarketplaceOrderPage(
            items=(invalid_payload,),
            next_cursor="cursor-new",
            watermark_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        )
    )

    with pytest.raises(ValueError, match="no external order identity"):
        sync_kaspi_order_page(
            session_factory,
            transport,
            marketplace_account_id=account_id,
        )

    with session_factory() as session:
        checkpoint = session.scalar(select(MarketplaceImportCheckpoint))
        orders = session.scalars(select(MarketplaceOrder)).all()
        execution = session.scalar(
            select(MarketplaceImportExecution).order_by(MarketplaceImportExecution.started_at.desc())
        )
        assert checkpoint is not None
        assert checkpoint.cursor == "cursor-old"
        assert checkpoint.watermark_at == old_watermark
        assert orders == []
        assert execution is not None
        assert execution.status == MarketplaceImportStatus.FAILED.value


def test_second_page_uses_committed_checkpoint(session_factory) -> None:
    account_id = _account_id(session_factory)
    first = FakeTransport(
        MarketplaceOrderPage(items=(_payload(),), next_cursor="cursor-2")
    )
    sync_kaspi_order_page(session_factory, first, marketplace_account_id=account_id)

    second = FakeTransport(
        MarketplaceOrderPage(
            items=(_payload(status="DELIVERED"),),
            next_cursor=None,
        )
    )
    result = sync_kaspi_order_page(
        session_factory,
        second,
        marketplace_account_id=account_id,
    )

    assert second.calls[0][0] == "cursor-2"
    assert result.imported_count == 0
    assert result.updated_count == 1
