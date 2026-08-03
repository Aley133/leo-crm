from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.dumping_models import DumpingPolicy, KaspiXmlFeed
from backend.app.dumping_service import (
    decide_dumping_price,
    publish_decision,
    resolve_cost_source,
    suspend_product_without_cost_source,
)
from backend.app.kaspi_competitor_agent_api import queue_competitor_job
from backend.app.models import Product
from backend.app.monitoring import SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_unavailable_supplier(db_session):
    now = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    product = Product(
        kaspi_product_id="987654321",
        merchant_sku="SKU-987654321",
        name="Товар без доступного поставщика",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="ozon-987654321",
        title="Карточка Ozon",
        url="https://www.ozon.ru/product/ozon-987654321/",
        current_price=Decimal("4998"),
        delivery_days=8,
        in_stock=True,
        last_checked_at=now,
    )
    db_session.add(supplier_product)
    db_session.flush()
    db_session.add(
        ProductBinding(
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            status="active",
            is_primary=True,
            priority=0,
        )
    )
    state = SupplierOfferState(
        supplier_product_id=supplier_product.id,
        price=None,
        old_price=None,
        currency="KZT",
        available=False,
        stock=0,
        delivery_days=None,
        seller=None,
        fingerprint="f" * 64,
        adapter_schema_version="ozon-browser-v13",
        observed_at=now,
        last_checked_at=now,
        version=2,
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
        minimum_profit_kzt=Decimal("2000"),
    )
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers>
      <offer sku='SKU-987654321'>
        <cityprices><cityprice cityId='750000000'>18189</cityprice></cityprices>
        <availability available='yes' preOrder='9'/>
      </offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml=xml,
        generated_xml=xml,
        active=True,
    )
    db_session.add_all([state, policy, feed])
    db_session.flush()
    return product, supplier_product, state, policy, feed


def test_confirmed_stock_loss_ignores_legacy_price_and_closes_xml(db_session) -> None:
    product, _supplier_product, _state, policy, feed = _seed_unavailable_supplier(
        db_session
    )

    assert resolve_cost_source(db_session, product_id=product.id) is None
    run = suspend_product_without_cost_source(
        db_session,
        product=product,
        policy=policy,
    )

    assert policy.enabled is True
    assert run.status == "suspended_no_source"
    assert run.published is True
    assert run.explanation_json["automatic_recovery"] is True
    assert 'available="no"' in feed.generated_xml
    assert 'preOrder="0"' in feed.generated_xml

    with pytest.raises(ValueError, match="демпинг приостановлен"):
        queue_competitor_job(
            db_session,
            product_id=product.id,
            reason="periodic_refresh",
        )


def test_available_source_reopens_xml_through_normal_dumping_publication(
    db_session,
) -> None:
    product, supplier_product, state, policy, feed = _seed_unavailable_supplier(
        db_session
    )
    suspend_product_without_cost_source(
        db_session,
        product=product,
        policy=policy,
    )
    state.price = Decimal("5000")
    state.available = True
    state.stock = 5
    state.delivery_days = 8
    state.fingerprint = "a" * 64
    supplier_product.current_price = Decimal("5000")
    supplier_product.in_stock = True
    supplier_product.delivery_days = 8
    db_session.flush()

    decision = decide_dumping_price(
        db_session,
        product=product,
        policy=policy,
        competitor_price_kzt=Decimal("16000"),
        own_price_kzt=Decimal("18189"),
    )
    publish_decision(
        db_session,
        product=product,
        policy=policy,
        decision=decision,
    )

    assert 'available="yes"' in feed.generated_xml
    assert 'preOrder="9"' in feed.generated_xml
    assert 'stockCount="5"' in feed.generated_xml


def test_newest_duplicate_supplier_state_overrides_stale_available_price(
    db_session,
) -> None:
    old_checked = datetime(2026, 7, 30, 2, 45, tzinfo=UTC)
    new_checked = datetime(2026, 7, 31, 13, 59, tzinfo=UTC)
    product = Product(
        kaspi_product_id="151877903",
        merchant_sku="151877903_110734483",
        name="GLS Pharmaceuticals Аргинин",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    stale = SupplierProduct(
        supplier_id=supplier.id,
        external_id="legacy-arginin",
        title="Аргинин Ozon",
        url="https://www.ozon.ru/product/arginin-51853964/",
        current_price=Decimal("4998"),
        in_stock=True,
        last_checked_at=old_checked,
    )
    fresh = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title="Аргинин Ozon",
        url="https://www.ozon.kz/product/arginin-51853964/",
        current_price=None,
        in_stock=False,
        last_checked_at=new_checked,
    )
    db_session.add_all([stale, fresh])
    db_session.flush()
    db_session.add_all(
        [
            ProductBinding(
                product_id=product.id,
                supplier_product_id=stale.id,
                status="active",
                is_primary=False,
                priority=0,
            ),
            ProductBinding(
                product_id=product.id,
                supplier_product_id=fresh.id,
                status="active",
                is_primary=True,
                priority=0,
            ),
            SupplierOfferState(
                supplier_product_id=stale.id,
                price=Decimal("4998"),
                currency="KZT",
                available=True,
                fingerprint="1" * 64,
                adapter_schema_version="ozon-browser-v12",
                observed_at=old_checked,
                last_checked_at=old_checked,
            ),
            SupplierOfferState(
                supplier_product_id=fresh.id,
                price=None,
                currency="KZT",
                available=False,
                stock=0,
                fingerprint="2" * 64,
                adapter_schema_version="ozon-browser-v13",
                observed_at=new_checked,
                last_checked_at=new_checked,
            ),
        ]
    )
    db_session.flush()

    assert resolve_cost_source(db_session, product_id=product.id) is None
