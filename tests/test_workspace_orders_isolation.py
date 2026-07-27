from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.app.commerce.repository import SqlAlchemyCommerceRepository
from backend.app.main import app
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceProvider
from backend.app.workspace_models import Workspace


def test_workspace_orders_route_is_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/orders" in paths


def test_commerce_repository_returns_only_owned_workspace_orders(db_session) -> None:
    first_workspace = Workspace(name="First orders", slug="first-orders")
    second_workspace = Workspace(name="Second orders", slug="second-orders")
    db_session.add_all([first_workspace, second_workspace])
    db_session.flush()

    first_account = MarketplaceAccount(
        workspace_id=first_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-first",
        display_name="First shop",
        timezone="Asia/Almaty",
    )
    second_account = MarketplaceAccount(
        workspace_id=second_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-second",
        display_name="Second shop",
        timezone="Asia/Almaty",
    )
    db_session.add_all([first_account, second_account])
    db_session.flush()

    first_order = MarketplaceOrder(
        marketplace_account_id=first_account.id,
        external_order_id="first-order-id",
        external_code="100000001",
        status="preorder",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("10000"),
        ordered_at=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
    )
    second_order = MarketplaceOrder(
        marketplace_account_id=second_account.id,
        external_order_id="second-order-id",
        external_code="200000002",
        status="preorder",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("20000"),
        ordered_at=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
    )
    db_session.add_all([first_order, second_order])
    db_session.commit()

    first_total, first_orders = SqlAlchemyCommerceRepository(
        db_session,
        workspace_id=first_workspace.id,
    ).list_orders(limit=50, offset=0)
    second_total, second_orders = SqlAlchemyCommerceRepository(
        db_session,
        workspace_id=second_workspace.id,
    ).list_orders(limit=50, offset=0)

    assert first_total == 1
    assert [order.external_code for order in first_orders] == ["100000001"]
    assert second_total == 1
    assert [order.external_code for order in second_orders] == ["200000002"]


def test_legacy_commerce_repository_still_reads_all_orders(db_session) -> None:
    workspace = Workspace(name="Legacy compatibility", slug="legacy-compatibility")
    db_session.add(workspace)
    db_session.flush()
    account = MarketplaceAccount(
        workspace_id=workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="legacy-partner",
        display_name="Legacy shop",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    db_session.add(
        MarketplaceOrder(
            marketplace_account_id=account.id,
            external_order_id="legacy-order-id",
            external_code="300000003",
            status="preorder",
            original_status="ACCEPTED_BY_MERCHANT",
            currency="KZT",
            total_amount=Decimal("30000"),
            ordered_at=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        )
    )
    db_session.commit()

    total, orders = SqlAlchemyCommerceRepository(db_session).list_orders(limit=50, offset=0)

    assert total >= 1
    assert "300000003" in {order.external_code for order in orders}
