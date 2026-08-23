from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from backend.app.inventory_models import InventoryBatch
from backend.app.models import MarketplaceAccount, Product
from backend.app.revenue_api import list_daily_revenue
from backend.app.revenue_models import DailyRevenueSnapshot
from backend.app.workspace_context import workspace_context


def _account(db_session, *, workspace_id: int, suffix: str) -> MarketplaceAccount:
    account = MarketplaceAccount(
        workspace_id=workspace_id,
        provider="kaspi",
        external_account_id=f"analytics-{suffix}",
        display_name=f"Analytics {suffix}",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    return account


def _product(db_session, *, workspace_id: int, suffix: str) -> Product:
    product = Product(
        workspace_id=workspace_id,
        kaspi_product_id=f"product-{suffix}",
        merchant_sku=f"SKU-{suffix}",
        name=f"Product {suffix}",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def test_revenue_analytics_is_workspace_scoped_and_values_stock(db_session):
    account_1 = _account(db_session, workspace_id=1, suffix="one")
    account_2 = _account(db_session, workspace_id=2, suffix="two")
    product_1 = _product(db_session, workspace_id=1, suffix="one")
    product_2 = _product(db_session, workspace_id=2, suffix="two")

    db_session.add_all(
        [
            DailyRevenueSnapshot(
                workspace_id=1,
                marketplace_account_id=account_1.id,
                business_date=date(2026, 8, 23),
                orders_count=4,
                units_count=5,
                revenue=Decimal("40000.00"),
                net_profit=Decimal("12000.00"),
                margin_pct=Decimal("30.00"),
                order_ids=[1, 2, 3, 4],
            ),
            DailyRevenueSnapshot(
                workspace_id=2,
                marketplace_account_id=account_2.id,
                business_date=date(2026, 8, 23),
                orders_count=99,
                units_count=99,
                revenue=Decimal("990000.00"),
                net_profit=Decimal("99000.00"),
                margin_pct=Decimal("10.00"),
                order_ids=[99],
            ),
            InventoryBatch(
                workspace_id=1,
                product_id=product_1.id,
                received_at=datetime(2026, 8, 1, tzinfo=UTC),
                quantity_received=10,
                quantity_remaining=6,
                unit_cost=Decimal("2500.00"),
                batch_type="purchase",
                is_received=True,
            ),
            InventoryBatch(
                workspace_id=1,
                product_id=product_1.id,
                received_at=datetime(2026, 8, 30, tzinfo=UTC),
                quantity_received=3,
                quantity_remaining=3,
                unit_cost=Decimal("2600.00"),
                batch_type="purchase",
                is_received=False,
            ),
            InventoryBatch(
                workspace_id=2,
                product_id=product_2.id,
                received_at=datetime(2026, 8, 1, tzinfo=UTC),
                quantity_received=100,
                quantity_remaining=100,
                unit_cost=Decimal("9999.00"),
                batch_type="purchase",
                is_received=True,
            ),
        ]
    )
    db_session.commit()

    with workspace_context(1):
        payload = list_daily_revenue(limit=30, db=db_session)

    assert payload["workspace_id"] == 1
    assert payload["total"] == 1
    assert payload["summary"]["revenue"] == Decimal("40000.00")
    assert payload["summary"]["net_profit"] == Decimal("12000.00")
    assert payload["inventory"]["on_hand_units"] == 6
    assert payload["inventory"]["on_hand_cost"] == Decimal("15000.00")
    assert payload["inventory"]["incoming_units"] == 3
    assert payload["inventory"]["incoming_cost"] == Decimal("7800.00")
    assert payload["inventory"]["sku_count"] == 1
    assert payload["inventory"]["top_capital"][0]["merchant_sku"] == "SKU-one"
