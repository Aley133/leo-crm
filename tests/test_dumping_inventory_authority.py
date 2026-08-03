from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from backend.app import dumping_events
from backend.app.dumping_runner import (
    apply_competitor_snapshot,
    refresh_dumping_for_supplier_product,
)
from backend.app.dumping_service import (
    decide_dumping_price,
    publish_decision,
    resolve_cost_source,
    sync_product_inventory_to_feed,
)
from backend.app.inventory_models import InventoryBatch, InventoryBatchType
from backend.app.inventory_service import allocate_order_line_fifo
from backend.app.kaspi_offer_competitor import KaspiCompetitorSnapshot
from backend.app.kaspi_competitor_agent_api import queue_competitor_job
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct
from backend.app.workspace_context import workspace_context


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _feed_xml(sku: str, *, stock_count: int = 10, preorder_days: int = 5) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers>
      <offer sku='{sku}'>
        <cityprices><cityprice cityId='750000000'>9999</cityprice></cityprices>
        <availability available='yes' preOrder='{preorder_days}' stockCount='{stock_count}'/>
      </offer>
    </offers></kaspi_catalog>"""


def _product(db_session, *, sku: str = "SKU-STOCK") -> Product:
    product = Product(
        kaspi_product_id=sku.removeprefix("SKU-"),
        merchant_sku=sku,
        name=f"Товар {sku}",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _feed(db_session, *, sku: str, stock_count: int = 10) -> KaspiXmlFeed:
    xml = _feed_xml(sku, stock_count=stock_count)
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml=xml,
        generated_xml=xml,
        active=True,
    )
    db_session.add(feed)
    db_session.flush()
    return feed


def _order_line(
    db_session,
    *,
    product: Product | None,
    sku: str,
    quantity: int = 1,
) -> tuple[MarketplaceOrder, MarketplaceOrderLine]:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id=f"merchant-{sku}",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    order = MarketplaceOrder(
        marketplace_account_id=0,
        external_order_id=f"order-{sku}",
        external_code=f"order-{sku}",
        status="accepted",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("9999") * quantity,
        ordered_at=NOW,
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id=f"line-{sku}",
        product_id=None if product is None else product.id,
        external_product_id=None if product is None else product.kaspi_product_id,
        merchant_sku=sku,
        title=f"Товар {sku}",
        quantity=quantity,
        unit_price=Decimal("9999"),
        line_total=Decimal("9999") * quantity,
    )
    order.lines.append(line)
    account.orders.append(order)
    db_session.add(account)
    db_session.flush()
    return order, line


def test_fifo_order_writes_exact_remaining_stock_to_xml(db_session) -> None:
    product = _product(db_session)
    feed = _feed(db_session, sku=product.merchant_sku or "")
    batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW,
        quantity_received=10,
        quantity_remaining=10,
        unit_cost=Decimal("4000"),
        source_name="Склад FIFO",
    )
    db_session.add(batch)
    order, line = _order_line(
        db_session,
        product=product,
        sku=product.merchant_sku or "",
    )

    allocate_order_line_fifo(db_session, order_line=line, order=order)

    assert batch.quantity_remaining == 9
    assert 'stockCount="9"' in feed.generated_xml
    assert 'preOrder="0"' in feed.generated_xml
    assert 'available="yes"' in feed.generated_xml


def test_supplier_preorder_recreates_offer_missing_from_xml(db_session) -> None:
    product = _product(db_session, sku="SKU-SUPPLIER-PREORDER")
    empty_xml = "<kaspi_catalog><offers/></kaspi_catalog>"
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml=empty_xml,
        generated_xml=empty_xml,
        active=True,
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([feed, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="supplier-preorder",
        title="Товар поставщика",
        url="https://www.ozon.ru/product/supplier-preorder/",
        current_price=Decimal("3295"),
        delivery_days=1,
        in_stock=True,
        last_checked_at=NOW,
    )
    db_session.add(supplier_product)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
        minimum_profit_kzt=Decimal("2000"),
        supplier_delivery_buffer_days=1,
    )
    db_session.add_all([binding, policy])
    db_session.flush()

    decision = decide_dumping_price(
        db_session,
        product=product,
        policy=policy,
        competitor_price_kzt=Decimal("9000"),
        own_price_kzt=None,
    )
    run = publish_decision(
        db_session,
        product=product,
        policy=policy,
        decision=decision,
    )

    assert decision.source.kind == "supplier"
    assert decision.source.unit_cost_kzt == Decimal("3295")
    assert decision.preorder_days == 2
    assert f'sku="{product.merchant_sku}"' in feed.generated_xml
    assert f">{int(decision.target_price_kzt)}<" in feed.generated_xml
    assert 'available="yes"' in feed.generated_xml
    assert 'preOrder="2"' in feed.generated_xml
    assert 'stockCount="0"' in feed.generated_xml
    assert run.explanation_json["xml_offer_recovered"] is True


def test_shared_sku_uses_supplier_binding_from_inventory_group(db_session) -> None:
    owner = _product(db_session, sku="SKU-SUPPLIER-OWNER")
    variant = _product(db_session, sku="SKU-SUPPLIER-VARIANT")
    variant.inventory_owner_product_id = owner.id
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add(supplier)
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="shared-supplier-source",
        title="Общий товар поставщика",
        url="https://www.ozon.ru/product/shared-supplier-source/",
        current_price=Decimal("3500"),
        delivery_days=3,
        in_stock=True,
        last_checked_at=NOW,
    )
    db_session.add(supplier_product)
    db_session.flush()
    db_session.add(
        ProductBinding(
            product_id=owner.id,
            supplier_product_id=supplier_product.id,
            status="active",
            is_primary=True,
            priority=0,
        )
    )
    policy = DumpingPolicy(
        product_id=variant.id,
        enabled=True,
        auto_publish_xml=True,
        supplier_delivery_buffer_days=1,
    )
    db_session.add(policy)
    db_session.flush()

    decision = decide_dumping_price(
        db_session,
        product=variant,
        policy=policy,
        competitor_price_kzt=Decimal("9000"),
        own_price_kzt=None,
    )

    assert decision.source.kind == "supplier"
    assert decision.source.unit_cost_kzt == Decimal("3500")
    assert decision.preorder_days == 4


def test_supplier_refresh_queues_dumping_for_every_shared_sku(
    db_session,
    monkeypatch,
) -> None:
    owner = _product(db_session, sku="SKU-REFRESH-OWNER")
    variant = _product(db_session, sku="SKU-REFRESH-VARIANT")
    variant.inventory_owner_product_id = owner.id
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add(supplier)
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="shared-refresh-source",
        title="Обновляемый общий товар",
        url="https://www.ozon.ru/product/shared-refresh-source/",
        current_price=Decimal("3600"),
        delivery_days=2,
        in_stock=True,
        last_checked_at=NOW,
    )
    db_session.add(supplier_product)
    db_session.flush()
    db_session.add_all(
        [
            ProductBinding(
                product_id=owner.id,
                supplier_product_id=supplier_product.id,
                status="active",
                is_primary=True,
                priority=0,
            ),
            DumpingPolicy(product_id=owner.id, enabled=True, auto_publish_xml=True),
            DumpingPolicy(product_id=variant.id, enabled=True, auto_publish_xml=True),
        ]
    )
    db_session.commit()

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    queued: list[int] = []
    monkeypatch.setattr("backend.app.dumping_runner.SessionLocal", factory)
    monkeypatch.setattr(
        "backend.app.dumping_runner.enqueue_competitor_scan",
        lambda product_id, *, reason: queued.append(int(product_id)),
    )

    refresh_dumping_for_supplier_product(
        supplier_product.id,
        workspace_id=1,
    )

    assert queued == [owner.id, variant.id]


def test_untracked_order_offer_is_closed_instead_of_republished(db_session) -> None:
    feed = _feed(db_session, sku="SKU-UNKNOWN")
    order, line = _order_line(
        db_session,
        product=None,
        sku="SKU-UNKNOWN",
    )

    result = allocate_order_line_fifo(db_session, order_line=line, order=order)

    assert result.newly_allocated_quantity == 0
    assert 'available="no"' in feed.generated_xml
    assert 'preOrder="0"' in feed.generated_xml
    assert 'stockCount="0"' in feed.generated_xml


def test_last_stock_unit_closes_offer_and_queues_fresh_supplier_check(db_session) -> None:
    product = _product(db_session, sku="SKU-PREORDER")
    feed = _feed(db_session, sku=product.merchant_sku or "", stock_count=1)
    batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW,
        quantity_received=1,
        quantity_remaining=1,
        unit_cost=Decimal("4000"),
        source_name="Склад FIFO",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([batch, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="ozon-preorder",
        title="Товар Ozon",
        url="https://www.ozon.ru/product/ozon-preorder/",
        current_price=Decimal("5000"),
        delivery_days=7,
        in_stock=True,
        last_checked_at=NOW,
    )
    db_session.add(supplier_product)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
        supplier_delivery_buffer_days=1,
    )
    state = SupplierOfferState(
        supplier_product_id=supplier_product.id,
        price=Decimal("5000"),
        currency="KZT",
        available=True,
        stock=5,
        delivery_days=7,
        fingerprint="f" * 64,
        adapter_schema_version="ozon-browser-v13",
        observed_at=NOW,
        last_checked_at=NOW,
    )
    db_session.add_all([binding, policy, state])
    db_session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=NOW,
        consecutive_failures=0,
        shard=0,
    )
    db_session.add(target)
    db_session.flush()
    order, line = _order_line(
        db_session,
        product=product,
        sku=product.merchant_sku or "",
    )

    allocate_order_line_fifo(db_session, order_line=line, order=order)

    assert batch.quantity_remaining == 0
    assert 'available="no"' in feed.generated_xml
    assert 'stockCount="0"' in feed.generated_xml
    queued = db_session.scalar(select(BrowserAgentJob))
    assert queued is not None
    assert queued.monitor_target_id == target.id
    waiting = db_session.scalar(
        select(DumpingRun).where(DumpingRun.status == "awaiting_supplier_refresh")
    )
    assert waiting is not None
    assert waiting.explanation_json["supplier_refresh_job_id"] == queued.id
    with pytest.raises(ValueError, match="свежая проверка поставщика"):
        queue_competitor_job(
            db_session,
            product_id=product.id,
            reason="periodic_refresh",
        )

    decision = decide_dumping_price(
        db_session,
        product=product,
        policy=policy,
        competitor_price_kzt=Decimal("9000"),
        own_price_kzt=Decimal("9999"),
    )
    publish_decision(db_session, product=product, policy=policy, decision=decision)

    assert decision.source.kind == "supplier"
    assert decision.preorder_days == 8
    assert decision.stock_count == 0
    assert 'available="yes"' in feed.generated_xml
    assert 'preOrder="8"' in feed.generated_xml
    assert 'stockCount="0"' in feed.generated_xml
    assert waiting.status == "supplier_refresh_applied"


def test_missing_own_kaspi_offer_keeps_dumping_and_republishes_xml(db_session) -> None:
    product = _product(db_session, sku="SKU-REMOVED")
    feed = _feed(db_session, sku=product.merchant_sku or "", stock_count=2)
    batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW,
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("4000"),
        source_name="Склад FIFO",
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
    )
    db_session.add_all([batch, policy])
    db_session.flush()
    db_session.add(
        DumpingRun(
            product_id=product.id,
            dumping_policy_id=policy.id,
            status="ready",
            own_price_kzt=Decimal("9999"),
            published=True,
            explanation_json={},
        )
    )
    db_session.flush()

    result = apply_competitor_snapshot(
        db_session,
        product_id=product.id,
        market=KaspiCompetitorSnapshot(
            own_price_kzt=None,
            competitor_price_kzt=Decimal("9000"),
            competitor_name="Другой продавец",
            own_position=None,
            seller_count=3,
            product_url="https://kaspi.kz/shop/p/removed/",
        ),
    )

    assert result["decision"]["status"] == "ready"
    assert policy.enabled is True
    assert 'available="yes"' in feed.generated_xml
    assert 'stockCount="2"' in feed.generated_xml
    latest = db_session.get(DumpingRun, result["run_id"])
    assert latest.explanation_json["own_offer_missing"] is True
    assert latest.explanation_json["own_offer_action"] == "keep_dumping_and_republish_xml"

    sync_product_inventory_to_feed(
        db_session,
        product_id=product.id,
        reason="later_inventory_change",
    )
    assert 'available="yes"' in feed.generated_xml


def test_first_publication_is_not_mistaken_for_manual_seller_removal(db_session) -> None:
    product = _product(db_session, sku="SKU-FIRST-PUBLISH")
    feed = _feed(db_session, sku=product.merchant_sku or "", stock_count=2)
    batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW,
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("4000"),
        source_name="Склад FIFO",
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
    )
    db_session.add_all([batch, policy])
    db_session.flush()

    result = apply_competitor_snapshot(
        db_session,
        product_id=product.id,
        market=KaspiCompetitorSnapshot(
            own_price_kzt=None,
            competitor_price_kzt=Decimal("9000"),
            competitor_name="Другой продавец",
            own_position=None,
            seller_count=3,
            product_url="https://kaspi.kz/shop/p/first-publication/",
        ),
    )

    assert result["decision"]["status"] != "suspended_seller_removed"
    assert policy.enabled is True
    assert 'available="yes"' in feed.generated_xml
    assert 'stockCount="2"' in feed.generated_xml


def test_production_capacity_is_not_counted_as_physical_stock(db_session) -> None:
    product = _product(db_session, sku="SKU-PRODUCTION")
    db_session.add(
        InventoryBatch(
            product_id=product.id,
            received_at=NOW,
            quantity_received=5,
            quantity_remaining=5,
            unit_cost=Decimal("1000"),
            batch_type=InventoryBatchType.PRODUCTION.value,
            is_received=False,
            source_name="Производство",
        )
    )
    db_session.flush()

    assert resolve_cost_source(db_session, product_id=product.id) is None


def test_supplier_refresh_keeps_originating_workspace_after_commit(
    db_session,
    monkeypatch,
) -> None:
    submitted: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    class InlineRecorder:
        def submit(self, function, *args, **kwargs):
            submitted.append((function, args, kwargs))

    monkeypatch.setattr(dumping_events, "_EXECUTOR", InlineRecorder())
    with workspace_context(2):
        dumping_events.mark_supplier_product_for_dumping_refresh(db_session, 77)

    dumping_events._run_dumping_after_commit(db_session)

    assert submitted == [
        (
            dumping_events.refresh_dumping_for_supplier_product,
            (77,),
            {"workspace_id": 2},
        )
    ]
