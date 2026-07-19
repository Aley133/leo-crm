from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine
from backend.app.product_identity_models import (
    MarketplaceListing,
    MarketplaceListingIssue,
    MarketplaceListingIssueReason,
    MarketplaceListingIssueStatus,
    MarketplaceListingStatus,
)
from backend.app.product_identity_service import (
    ensure_marketplace_listing_for_order_line,
    select_listing_identity,
)


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_line(factory, *, merchant_sku: str | None, external_product_id: str | None):
    with factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider="kaspi",
                external_account_id="partner-identity",
                display_name="LEO",
                timezone="Asia/Almaty",
            )
            session.add(account)
            session.flush()
            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id="order-identity",
                external_code="999000111",
                status="accepted",
                original_status="ACCEPTED_BY_MERCHANT",
                currency="KZT",
                total_amount=Decimal("4999.00"),
                version=1,
            )
            line = MarketplaceOrderLine(
                external_line_id="line-identity",
                merchant_sku=merchant_sku,
                external_product_id=external_product_id,
                title="Identity product",
                quantity=1,
                unit_price=Decimal("4999.00"),
                line_total=Decimal("4999.00"),
            )
            order.lines.append(line)
            session.add(order)
            session.flush()
            return account.id, line.id


def test_identity_prefers_non_blank_merchant_sku_and_namespaces_key() -> None:
    identity = select_listing_identity(
        merchant_sku="  SKU-1  ",
        external_product_id="product-1",
    )

    assert identity is not None
    assert identity.kind == "merchant_sku"
    assert identity.raw_value == "SKU-1"
    assert identity.identity_key == "merchant_sku:SKU-1"


def test_listing_creation_is_idempotent_with_on_conflict() -> None:
    engine, factory = _factory()
    try:
        account_id, line_id = _seed_line(
            factory,
            merchant_sku="SKU-ONE",
            external_product_id="product-one",
        )
        with factory() as session:
            with session.begin():
                line = session.get(MarketplaceOrderLine, line_id)
                first = ensure_marketplace_listing_for_order_line(
                    session,
                    marketplace_account_id=account_id,
                    order_line=line,
                )
                second = ensure_marketplace_listing_for_order_line(
                    session,
                    marketplace_account_id=account_id,
                    order_line=line,
                )

                assert first.listing_id == second.listing_id
                assert first.missing_identity is False
                assert session.scalar(select(func.count(MarketplaceListing.id))) == 1
                listing = session.get(MarketplaceListing, first.listing_id)
                assert listing.identity_key == "merchant_sku:SKU-ONE"
                assert listing.status == MarketplaceListingStatus.UNRESOLVED.value
                assert listing.product_id is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_missing_identity_creates_issue_but_never_empty_listing() -> None:
    engine, factory = _factory()
    try:
        account_id, line_id = _seed_line(
            factory,
            merchant_sku="   ",
            external_product_id=None,
        )
        with factory() as session:
            with session.begin():
                line = session.get(MarketplaceOrderLine, line_id)
                result = ensure_marketplace_listing_for_order_line(
                    session,
                    marketplace_account_id=account_id,
                    order_line=line,
                )

                assert result.listing_id is None
                assert result.issue_id is not None
                assert result.missing_identity is True
                assert session.scalar(select(func.count(MarketplaceListing.id))) == 0
                issue = session.get(MarketplaceListingIssue, result.issue_id)
                assert issue.reason == MarketplaceListingIssueReason.MISSING_IDENTITY.value
                assert issue.status == MarketplaceListingIssueStatus.OPEN.value
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_later_identity_resolves_issue_without_deleting_history() -> None:
    engine, factory = _factory()
    try:
        account_id, line_id = _seed_line(
            factory,
            merchant_sku=None,
            external_product_id=None,
        )
        with factory() as session:
            with session.begin():
                line = session.get(MarketplaceOrderLine, line_id)
                first = ensure_marketplace_listing_for_order_line(
                    session,
                    marketplace_account_id=account_id,
                    order_line=line,
                )
                issue_id = first.issue_id
                line.external_product_id = "external-later"
                second = ensure_marketplace_listing_for_order_line(
                    session,
                    marketplace_account_id=account_id,
                    order_line=line,
                )

                assert second.listing_id is not None
                issue = session.get(MarketplaceListingIssue, issue_id)
                assert issue.status == MarketplaceListingIssueStatus.RESOLVED.value
                assert issue.resolved_at is not None
                assert session.scalar(select(func.count(MarketplaceListingIssue.id))) == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
