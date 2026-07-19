from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product
from backend.app.product_identity_models import (
    MarketplaceListing,
    MarketplaceListingEvent,
    MarketplaceListingStatus,
)
from backend.app.product_resolver import resolve_listing, unresolve_listing


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed(factory):
    with factory() as session:
        with session.begin():
            account = MarketplaceAccount(
                provider="kaspi",
                external_account_id="resolver-account",
                display_name="LEO",
                timezone="Asia/Almaty",
            )
            product_a = Product(
                kaspi_product_id="109751709",
                merchant_sku="SKU-RESOLVE",
                name="Product A",
                status="active",
            )
            product_b = Product(
                kaspi_product_id="109751710",
                merchant_sku="SKU-OTHER",
                name="Product B",
                status="active",
            )
            session.add_all([account, product_a, product_b])
            session.flush()

            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id="resolver-order",
                external_code="999975446",
                status="accepted",
                original_status="ACCEPTED_BY_MERCHANT",
                currency="KZT",
                total_amount=Decimal("4999.00"),
                version=1,
            )
            line = MarketplaceOrderLine(
                external_line_id="resolver-line",
                merchant_sku="SKU-RESOLVE",
                external_product_id="109751709",
                title="Resolver product",
                quantity=1,
                unit_price=Decimal("4999.00"),
                line_total=Decimal("4999.00"),
            )
            order.lines.append(line)
            session.add(order)
            session.flush()

            listing = MarketplaceListing(
                marketplace_account_id=account.id,
                identity_kind="merchant_sku",
                identity_key="merchant_sku:SKU-RESOLVE",
                merchant_sku="SKU-RESOLVE",
                external_product_id="109751709",
                status="unresolved",
            )
            session.add(listing)
            session.flush()
            return listing.id, line.id, product_a.id, product_b.id


def test_resolve_reassign_and_unresolve_are_audited() -> None:
    engine, factory = _factory()
    try:
        listing_id, line_id, product_a_id, product_b_id = _seed(factory)
        with factory() as session:
            with session.begin():
                listing = resolve_listing(
                    session,
                    listing_id=listing_id,
                    product_id=product_a_id,
                    actor="test",
                )
                assert listing.status == MarketplaceListingStatus.RESOLVED.value
                assert session.get(MarketplaceOrderLine, line_id).product_id == product_a_id

                resolve_listing(
                    session,
                    listing_id=listing_id,
                    product_id=product_b_id,
                    actor="test",
                )
                assert session.get(MarketplaceOrderLine, line_id).product_id == product_b_id

                unresolve_listing(session, listing_id=listing_id, actor="test")
                assert session.get(MarketplaceOrderLine, line_id).product_id is None
                assert session.get(MarketplaceListing, listing_id).status == "unresolved"

                events = session.scalars(
                    select(MarketplaceListingEvent)
                    .where(MarketplaceListingEvent.marketplace_listing_id == listing_id)
                    .order_by(MarketplaceListingEvent.id)
                ).all()
                assert [event.event_type for event in events] == [
                    "resolved",
                    "reassigned",
                    "unresolved",
                ]
                assert session.scalar(select(func.count(MarketplaceListingEvent.id))) == 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_resolving_same_product_is_idempotent() -> None:
    engine, factory = _factory()
    try:
        listing_id, _, product_a_id, _ = _seed(factory)
        with factory() as session:
            with session.begin():
                resolve_listing(session, listing_id=listing_id, product_id=product_a_id)
                resolve_listing(session, listing_id=listing_id, product_id=product_a_id)
                assert session.scalar(select(func.count(MarketplaceListingEvent.id))) == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
