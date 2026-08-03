from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import sessionmaker

from backend.app import kaspi_product_enrichment_jobs
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceProvider,
    Product,
)


def test_automatic_enrichment_accepts_flat_kaspi_product_responses(
    db_session,
    monkeypatch,
) -> None:
    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="1143018",
        display_name="Kaspi",
    )
    db_session.add(account)
    db_session.flush()
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id="order-internal-1014396386",
        external_code="1014396386",
        status="preorder",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("7989"),
        ordered_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    order.lines.append(
        MarketplaceOrderLine(
            external_line_id="entry-1014396386-0",
            external_product_id="master-1014396386",
            merchant_sku=None,
            title="Unknown product",
            quantity=1,
            unit_price=Decimal("7989"),
            line_total=Decimal("7989"),
        )
    )
    db_session.add(order)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(kaspi_product_enrichment_jobs, "SessionLocal", factory)
    monkeypatch.setenv("KASPI_API_TOKEN", "test-token")
    monkeypatch.setenv("KASPI_API_BASE_URL", "https://kaspi.example")

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/orders/order-internal-1014396386/entries":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "entry-1014396386-0",
                            "type": "orderentries",
                            "attributes": {
                                "quantity": 1,
                                "basePrice": 7989,
                                "totalPrice": 7989,
                            },
                            "relationships": {
                                "product": {
                                    "data": {
                                        "id": "master-1014396386",
                                        "type": "masterproducts",
                                    }
                                }
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/orderentries/entry-1014396386-0/product":
            return httpx.Response(
                200,
                json={
                    "id": "master-1014396386",
                    "productName": "Резервное название товара",
                },
            )
        if request.url.path == "/masterproducts/master-1014396386/merchantProduct":
            return httpx.Response(
                200,
                json={
                    "productName": "Точное название нового товара",
                    "merchantSku": "1014396386_SKU",
                },
            )
        return httpx.Response(404, json={"error": "unexpected request"})

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(kaspi_product_enrichment_jobs.httpx, "AsyncClient", client_factory)

    job_id = kaspi_product_enrichment_jobs.create_job(days=1)
    asyncio.run(kaspi_product_enrichment_jobs.run_job(job_id))

    with factory() as session:
        stored = session.get(MarketplaceOrder, order.id)
        assert stored is not None
        assert len(stored.lines) == 1
        assert stored.lines[0].title == "Точное название нового товара"
        assert stored.lines[0].merchant_sku == "1014396386_SKU"
        assert stored.lines[0].external_product_id == "master-1014396386"

    job = kaspi_product_enrichment_jobs.public_job(job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["updated"] == 1
    assert requests == [
        "/orders/order-internal-1014396386/entries",
        "/orderentries/entry-1014396386-0/product",
        "/masterproducts/master-1014396386/merchantProduct",
    ]


def test_registry_resolves_new_order_before_eventually_consistent_kaspi_endpoints(
    db_session,
    monkeypatch,
) -> None:
    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="11843018",
        display_name="Kaspi",
    )
    product = Product(
        kaspi_product_id="102591425_901104670",
        merchant_sku="102591425_901104670",
        name='GLS Pharmaceuticals "Климмикс" капсулы 60 шт',
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id="order-internal-1020953791",
        external_code="1020953791",
        status="preorder",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("4245"),
        ordered_at=datetime.now(UTC) - timedelta(hours=2),
    )
    order.lines.append(
        MarketplaceOrderLine(
            external_line_id="entry-1020953791-0",
            external_product_id="102591425",
            merchant_sku=None,
            title="Unknown product",
            quantity=1,
            unit_price=Decimal("4245"),
            line_total=Decimal("4245"),
        )
    )
    db_session.add(order)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(kaspi_product_enrichment_jobs, "SessionLocal", factory)

    resolved, updated, linked = (
        kaspi_product_enrichment_jobs._resolve_stored_order_from_registry(
            order_id=order.id,
            account_id=account.id,
        )
    )

    with factory() as session:
        stored = session.get(MarketplaceOrder, order.id)
        assert stored is not None
        line = stored.lines[0]
        assert line.product_id == product.id
        assert line.merchant_sku == product.merchant_sku
        assert line.title == product.name
    assert resolved is True
    assert updated == 1
    assert linked == 1


def test_unresolved_backlog_remains_eligible_after_fast_poll_window(
    db_session,
    monkeypatch,
) -> None:
    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="11843018",
        display_name="Kaspi",
    )
    db_session.add(account)
    db_session.flush()
    old_order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id="old-unresolved-order",
        external_code="1020953791",
        status="preorder",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("4245"),
        ordered_at=datetime.now(UTC) - timedelta(days=10),
    )
    old_order.lines.append(
        MarketplaceOrderLine(
            external_line_id="old-unresolved-line",
            external_product_id="102591425",
            title="Unknown product",
            quantity=1,
            unit_price=Decimal("4245"),
            line_total=Decimal("4245"),
        )
    )
    db_session.add(old_order)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(kaspi_product_enrichment_jobs, "SessionLocal", factory)

    selected = kaspi_product_enrichment_jobs._load_unresolved_orders(
        datetime.now(UTC) - timedelta(days=31),
        marketplace_account_id=account.id,
        limit=16,
    )

    assert [row[0] for row in selected] == [old_order.id]
