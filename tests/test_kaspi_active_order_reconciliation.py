from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import kaspi_active_order_reconciliation, kaspi_raw_receiver_jobs
from backend.app.db import Base
from backend.app.models import MarketplaceAccount, MarketplaceOrder


class ExactCodeTransport:
    def __init__(self, payloads: dict[str, dict | None]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []
        self.closed = False

    def fetch_order_by_code(self, order_code: str):
        self.calls.append(order_code)
        return self.payloads.get(order_code)

    def close(self) -> None:
        self.closed = True


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _order(
    account: MarketplaceAccount,
    *,
    code: str,
    status: str,
    days_ago: int,
) -> MarketplaceOrder:
    return MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=f"external-{code}",
        external_code=code,
        status=status,
        original_status=status.upper(),
        currency="KZT",
        total_amount=Decimal("5000"),
        ordered_at=datetime(2026, 8, 3, tzinfo=UTC) - timedelta(days=days_ago),
        version=1,
    )


def test_old_nonterminal_order_is_reconciled_outside_creation_window(monkeypatch) -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            with session.begin():
                account = MarketplaceAccount(
                    provider="kaspi",
                    external_account_id="merchant-old-order",
                    display_name="BARWORK",
                    timezone="Asia/Almaty",
                )
                session.add(account)
                session.flush()
                account_id = account.id
                session.add_all(
                    [
                        _order(account, code="1020000001", status="assembly", days_ago=30),
                        _order(account, code="1020000002", status="delivered", days_ago=31),
                    ]
                )
                other_account = MarketplaceAccount(
                    provider="kaspi",
                    external_account_id="merchant-other-workspace",
                    display_name="LeoXpress",
                    timezone="Asia/Almaty",
                )
                session.add(other_account)
                session.flush()
                session.add(
                    _order(
                        other_account,
                        code="1020000003",
                        status="assembly",
                        days_ago=45,
                    )
                )

        transport = ExactCodeTransport(
            {
                "1020000001": {
                    "id": "external-1020000001",
                    "attributes": {
                        "code": "1020000001",
                        "status": "ARCHIVED",
                        "state": "ARCHIVE",
                        "currency": "KZT",
                        "totalPrice": "5000",
                        "creationDate": int(
                            datetime(2026, 7, 4, tzinfo=UTC).timestamp() * 1000
                        ),
                    },
                }
            }
        )
        connection = SimpleNamespace(
            account_id=account_id,
            timezone="Asia/Almaty",
            transport=lambda **_options: transport,
        )
        monkeypatch.setattr(kaspi_active_order_reconciliation, "SessionLocal", factory)
        monkeypatch.setattr(kaspi_raw_receiver_jobs, "SessionLocal", factory)

        result = asyncio.run(
            kaspi_active_order_reconciliation.reconcile_active_orders(connection)
        )

        assert result.checked == 1
        assert result.found == 1
        assert result.updated == 1
        assert transport.calls == ["1020000001"]
        assert transport.closed is True
        with factory() as session:
            repaired = session.scalar(
                select(MarketplaceOrder).where(
                    MarketplaceOrder.external_code == "1020000001"
                )
            )
            terminal = session.scalar(
                select(MarketplaceOrder).where(
                    MarketplaceOrder.external_code == "1020000002"
                )
            )
            assert repaired is not None and repaired.status == "delivered"
            assert terminal is not None and terminal.status == "delivered"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_one_exact_lookup_failure_does_not_block_other_active_orders(monkeypatch) -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            with session.begin():
                account = MarketplaceAccount(
                    provider="kaspi",
                    external_account_id="merchant-partial-repair",
                    display_name="LeoXpress",
                    timezone="Asia/Almaty",
                )
                session.add(account)
                session.flush()
                account_id = account.id
                session.add_all(
                    [
                        _order(account, code="1020000010", status="assembly", days_ago=12),
                        _order(account, code="1020000011", status="shipping", days_ago=14),
                    ]
                )

        class PartialTransport(ExactCodeTransport):
            def fetch_order_by_code(self, order_code: str):
                self.calls.append(order_code)
                if order_code == "1020000010":
                    raise TimeoutError("temporary Kaspi timeout")
                return {
                    "id": f"external-{order_code}",
                    "attributes": {
                        "code": order_code,
                        "status": "COMPLETED",
                        "state": "ARCHIVE",
                        "currency": "KZT",
                        "totalPrice": "5000",
                    },
                }

        transport = PartialTransport({})
        connection = SimpleNamespace(
            account_id=account_id,
            timezone="Asia/Almaty",
            transport=lambda **_options: transport,
        )
        monkeypatch.setattr(kaspi_active_order_reconciliation, "SessionLocal", factory)
        monkeypatch.setattr(kaspi_raw_receiver_jobs, "SessionLocal", factory)

        result = asyncio.run(
            kaspi_active_order_reconciliation.reconcile_active_orders(connection)
        )

        assert result.checked == 2
        assert result.found == 1
        assert result.updated == 1
        assert len(result.errors) == 1
        with factory() as session:
            repaired = session.scalar(
                select(MarketplaceOrder).where(
                    MarketplaceOrder.external_code == "1020000011"
                )
            )
            assert repaired is not None and repaired.status == "delivered"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
