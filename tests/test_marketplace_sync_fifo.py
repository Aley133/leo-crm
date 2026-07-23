from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import create_inventory_batch
from backend.app.marketplace_sync import sync_kaspi_order_page
from backend.app.marketplace_transport import MarketplaceOrderPage
from backend.app.models import MarketplaceAccount, MarketplaceProvider, Product


class FakeTransport:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def fetch_orders(
        self,
        *,
        cursor: str | None,
        updated_after: datetime | None,
        limit: int,
    ) -> MarketplaceOrderPage:
        return MarketplaceOrderPage(items=(self.payload,), next_cursor=None)


def _factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _payload() -> dict:
    return {
        "id": "order-after-stock",
        "attributes": {
            "code": "1009000001",
            "status": "NEW",
            "revision": "rev-1",
            "currency": "KZT",
            "totalPrice": "3600.00",
            "creationDate": "2026-07-22T10:00:00Z",
            "updatedAt": "2026-07-22T10:05:00Z",
            "entries": [
                {
                    "id": "line-after-stock",
                    "attributes": {
                        "offerCode": "102656018_307802943",
                        "name": "GLS Pharmaceuticals Магний цитрат",
                        "quantity": 1,
                        "basePrice": "3600.00",
                        "totalPrice": "3600.00",
                    },
                }
            ],
        },
    }


def test_new_kaspi_order_consumes_existing_fifo_stock_once() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            with session.begin():
                account = MarketplaceAccount(
                    provider=MarketplaceProvider.KASPI.value,
                    external_account_id="11843018",
                    display_name="Kaspi Shop",
                    timezone="Asia/Almaty",
                )
                product = Product(
                    kaspi_product_id="102656018",
                    merchant_sku="102656018_307802943",
                    name="GLS Pharmaceuticals Магний цитрат",
                    status="active",
                )
                session.add_all([account, product])
                session.flush()
                account_id = account.id
                batch, _ = create_inventory_batch(
                    session,
                    product=product,
                    quantity=12,
                    unit_cost=Decimal("2300"),
                    received_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
                    source_name="OZON",
                    reconcile_existing_orders=False,
                )
                batch_id = batch.id

        transport = FakeTransport(_payload())
        sync_kaspi_order_page(factory, transport, marketplace_account_id=account_id)
        sync_kaspi_order_page(factory, transport, marketplace_account_id=account_id)

        with factory() as session:
            batch = session.get(InventoryBatch, batch_id)
            assert batch is not None
            assert batch.quantity_remaining == 11
            allocated = session.scalar(
                select(func.coalesce(func.sum(InventoryAllocation.quantity), 0))
            )
            assert int(allocated or 0) == 1
            allocations = session.scalars(select(InventoryAllocation)).all()
            assert len(allocations) == 1
            assert Decimal(allocations[0].unit_cost) == Decimal("2300")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
