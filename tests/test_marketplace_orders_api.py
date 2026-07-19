from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import marketplace_orders_api
from backend.app.db import Base
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderLine,
)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed(factory) -> int:
    with factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider="kaspi",
                external_account_id="partner-1",
                display_name="LEO",
                timezone="Asia/Almaty",
            )
            session.add(account)
            session.flush()
            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id="order-1",
                external_code="996801988",
                status="accepted",
                original_status="ACCEPTED_BY_MERCHANT",
                currency="KZT",
                total_amount=Decimal("15990.00"),
                ordered_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
                version=2,
            )
            order.lines.extend(
                [
                    MarketplaceOrderLine(
                        external_line_id="line-1",
                        external_product_id="product-1",
                        merchant_sku="SKU-OMEGA",
                        title="Omega 3",
                        quantity=2,
                        unit_price=Decimal("4995.00"),
                        line_total=Decimal("9990.00"),
                    ),
                    MarketplaceOrderLine(
                        external_line_id="line-2",
                        external_product_id="product-2",
                        merchant_sku="SKU-D3",
                        title="Vitamin D3",
                        quantity=1,
                        unit_price=Decimal("6000.00"),
                        line_total=Decimal("6000.00"),
                    ),
                ]
            )
            order.events.append(
                MarketplaceOrderEvent(
                    source_event_key="status:v2:accepted",
                    event_type="status_changed",
                    previous_status="new",
                    current_status="accepted",
                    occurred_at=datetime(2026, 7, 19, 10, 5, tzinfo=UTC),
                    metadata_json={"original_status": "ACCEPTED_BY_MERCHANT"},
                )
            )
            session.add(order)
            session.flush()
            return order.id


def test_list_orders_exposes_operational_summary_and_sku_search(monkeypatch) -> None:
    engine, factory = _factory()
    try:
        order_id = _seed(factory)
        monkeypatch.setattr(marketplace_orders_api, "SessionLocal", factory)

        response = marketplace_orders_api.list_marketplace_orders(
            limit=50,
            offset=0,
            order_status="accepted",
            query="SKU-OMEGA",
            include_lines=False,
        )

        assert response["total"] == 1
        item = response["items"][0]
        assert item["id"] == order_id
        assert item["external_code"] == "996801988"
        assert item["total_amount"] == "15990.00"
        assert item["line_count"] == 2
        assert item["total_quantity"] == 3
        assert item["hydrated"] is True
        assert "lines" not in item
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_list_orders_can_expand_hydrated_lines(monkeypatch) -> None:
    engine, factory = _factory()
    try:
        _seed(factory)
        monkeypatch.setattr(marketplace_orders_api, "SessionLocal", factory)

        response = marketplace_orders_api.list_marketplace_orders(
            limit=50,
            offset=0,
            order_status=None,
            query="996801988",
            include_lines=True,
        )

        assert response["total"] == 1
        assert [line["merchant_sku"] for line in response["items"][0]["lines"]] == [
            "SKU-OMEGA",
            "SKU-D3",
        ]
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_order_detail_contains_lines_and_status_history(monkeypatch) -> None:
    engine, factory = _factory()
    try:
        order_id = _seed(factory)
        monkeypatch.setattr(marketplace_orders_api, "SessionLocal", factory)

        response = marketplace_orders_api.get_marketplace_order(order_id)

        assert [line["merchant_sku"] for line in response["lines"]] == [
            "SKU-OMEGA",
            "SKU-D3",
        ]
        assert response["events"][0]["previous_status"] == "new"
        assert response["events"][0]["current_status"] == "accepted"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_order_can_be_opened_by_human_facing_kaspi_code(monkeypatch) -> None:
    engine, factory = _factory()
    try:
        order_id = _seed(factory)
        monkeypatch.setattr(marketplace_orders_api, "SessionLocal", factory)

        response = marketplace_orders_api.get_marketplace_order_by_code("996801988")

        assert response["id"] == order_id
        assert response["external_code"] == "996801988"
        assert response["hydrated"] is True
        assert response["lines"][0]["title"] == "Omega 3"
        assert response["lines"][0]["quantity"] == 2
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
