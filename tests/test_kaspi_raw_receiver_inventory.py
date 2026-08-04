from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app import kaspi_raw_receiver_jobs
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.models import MarketplaceAccount, MarketplaceOrderLine, Product


def test_raw_entry_decodes_base64_kaspi_product_id() -> None:
    entry = kaspi_raw_receiver_jobs._flatten_entry(
        {
            "id": "entry-encoded-product",
            "attributes": {"quantity": 1, "basePrice": "3611"},
            "relationships": {
                "product": {
                    "data": {"type": "masterproducts", "id": "MTA1NTc5OTQx"}
                }
            },
        },
        {},
    )

    assert entry["attributes"]["productId"] == "105579941"
    assert entry["attributes"]["name"] == "Unknown product"


def test_raw_receiver_links_encoded_kaspi_id_and_allocates_without_waiting_for_enrichment(
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
        kaspi_product_id="105579941",
        merchant_sku="105579941_616168964",
        name="GLS Комплекс витаминов B",
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    batch = InventoryBatch(
        product_id=product.id,
        received_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        quantity_received=19,
        quantity_remaining=19,
        unit_cost=Decimal("2000"),
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

    entry = kaspi_raw_receiver_jobs._flatten_entry(
        {
            "id": "entry-1020159668",
            "attributes": {
                "quantity": 1,
                "basePrice": "3611",
                "totalPrice": "3611",
            },
            "relationships": {
                "product": {
                    "data": {"type": "masterproducts", "id": "MTA1NTc5OTQx"}
                }
            },
        },
        {},
    )
    payload = {
        "id": "order-1020159668",
        "attributes": {
            "code": "1020159668",
            "state": "KASPI_DELIVERY",
            "status": "ACCEPTED_BY_MERCHANT",
            "preOrder": False,
            "creationDate": int(
                datetime(2026, 8, 3, 0, 43, tzinfo=UTC).timestamp() * 1000
            ),
            "totalPrice": "3611",
            "currency": "KZT",
            "entries": [entry],
        },
    }

    imported, updated = kaspi_raw_receiver_jobs._persist_orders(
        [payload],
        timezone_name="Asia/Almaty",
    )

    assert (imported, updated) == (1, 0)
    with factory() as session:
        line = session.scalar(select(MarketplaceOrderLine))
        persisted_batch = session.get(InventoryBatch, batch_id)
        allocation = session.scalar(select(InventoryAllocation))
        assert line is not None
        assert line.external_product_id == "105579941"
        assert line.product_id == product.id
        assert line.title == "GLS Комплекс витаминов B"
        assert allocation is not None and allocation.quantity == 1
        assert persisted_batch is not None and persisted_batch.quantity_remaining == 18


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
    # a second unit from the batch or repeat FIFO work after Kaspi is already
    # persisted. Inventory-batch creation owns later reconciliation.
    def unexpected_fifo_rebuild(*_args, **_kwargs):
        raise AssertionError("unchanged order must not repeat FIFO allocation")

    monkeypatch.setattr(
        kaspi_raw_receiver_jobs,
        "allocate_order_line_fifo",
        unexpected_fifo_rebuild,
    )
    kaspi_raw_receiver_jobs._persist_orders([payload], timezone_name="Asia/Almaty")
    with factory() as session:
        allocations = session.scalars(select(InventoryAllocation)).all()
        persisted_batch = session.get(InventoryBatch, batch_id)
        assert len(allocations) == 1
        assert allocations[0].quantity == 1
        assert persisted_batch is not None
        assert persisted_batch.quantity_remaining == 11


def test_raw_receiver_restores_fifo_stock_after_confirmed_cancellation(
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
        quantity_received=1,
        quantity_remaining=1,
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
        "id": "order-cancelled-before-courier",
        "attributes": {
            "code": "1008415720",
            "state": "KASPI_DELIVERY",
            "status": "ACCEPTED_BY_MERCHANT",
            "preOrder": True,
            "creationDate": int(
                datetime(2026, 7, 23, 8, 0, tzinfo=UTC).timestamp() * 1000
            ),
            "totalPrice": "3600",
            "currency": "KZT",
            "entries": [
                {
                    "id": "entry-cancelled-before-courier",
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

    kaspi_raw_receiver_jobs._persist_orders([payload], timezone_name="Asia/Almaty")
    with factory() as session:
        assert session.get(InventoryBatch, batch_id).quantity_remaining == 0
        assert len(session.scalars(select(InventoryAllocation)).all()) == 1

    payload["attributes"]["status"] = "CANCELLED"
    kaspi_raw_receiver_jobs._persist_orders([payload], timezone_name="Asia/Almaty")
    kaspi_raw_receiver_jobs._persist_orders([payload], timezone_name="Asia/Almaty")

    with factory() as session:
        assert session.get(InventoryBatch, batch_id).quantity_remaining == 1
        assert session.scalars(select(InventoryAllocation)).all() == []
