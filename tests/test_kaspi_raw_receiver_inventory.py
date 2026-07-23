from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app import kaspi_raw_receiver_jobs
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.models import MarketplaceAccount, Product


def test_raw_receiver_persists_order_and_allocates_existing_fifo_stock(
    db_session,
    monkeypatch,
) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    product = Product(
        kaspi_product_id="102656018_307802943",
        merchant_sku="102656018_307802943",
        name="GLS Magnesium",
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    batch = InventoryBatch(
        product_id=product.id,
        received_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        quantity_received=12,
        quantity_remaining=12,
        unit_cost=Decimal("2300"),
        source_name="OZON",
    )
    db_session.add(batch)
    db_session.commit()

    account_id = account.id
    batch_id = batch.id
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(kaspi_raw_receiver_jobs, "SessionLocal", factory)
    monkeypatch.setattr(
        kaspi_raw_receiver_jobs,
        "ensure_kaspi_marketplace_account",
        lambda session: session.get(MarketplaceAccount, account_id),
    )

    payload = {
        "id": "order-new-1",
        "attributes": {
            "code": "1008415719",
            "state": "KASPI_DELIVERY",
            "status": "ACCEPTED_BY_MERCHANT",
            "preOrder": False,
            "creationDate": int(datetime(2026, 7, 23, 8, 0, tzinfo=UTC).timestamp() * 1000),
            "totalPrice": "3600",
            "currency": "KZT",
            "entries": [
                {
                    "id": "entry-new-1",
                    "attributes": {
                        "offerCode": "102656018_307802943",
                        "productId": "102656018_307802943",
                        "name": "GLS Magnesium",
                        "quantity": 1,
                        "basePrice": "3600",
                        "totalPrice": "3600",
                    },
                }
            ],
        },
    }

    imported, updated = kaspi_raw_receiver_jobs._persist_orders(
        [payload],
        timezone_name="Asia/Almaty",
    )

    assert imported == 1
    assert updated == 0
    with factory() as session:
        allocation = session.scalar(select(InventoryAllocation))
        persisted_batch = session.get(InventoryBatch, batch_id)
        assert allocation is not None
        assert allocation.quantity == 1
        assert persisted_batch is not None
        assert persisted_batch.quantity_remaining == 11

    # A manual rebuild may revisit the same unchanged order. It must not consume
    # a second unit from the batch.
    kaspi_raw_receiver_jobs._persist_orders([payload], timezone_name="Asia/Almaty")
    with factory() as session:
        allocations = session.scalars(select(InventoryAllocation)).all()
        persisted_batch = session.get(InventoryBatch, batch_id)
        assert len(allocations) == 1
        assert allocations[0].quantity == 1
        assert persisted_batch is not None
        assert persisted_batch.quantity_remaining == 11
