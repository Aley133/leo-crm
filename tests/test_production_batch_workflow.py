from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.commerce.domain import (
    CommerceOrder,
    CommerceOrderLine,
    CommerceOrderStage,
    ProcurementState,
)
from backend.app.commerce.repository import SqlAlchemyCommerceRepository
from backend.app.inventory_api import (
    _product_inventory,
    delete_product_inventory_batch,
)
from backend.app.inventory_models import (
    InventoryAllocation,
    InventoryBatchType,
)
from backend.app.inventory_service import (
    build_incoming_reservations,
    complete_production_order,
    create_inventory_batch,
    mark_inventory_batch_received,
    rebuild_product_fifo,
)
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    Product,
)


NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def _seed_product(db_session) -> Product:
    product = Product(
        kaspi_product_id="PRODUCTION-1",
        merchant_sku="PRODUCTION-1",
        name="Товар собственного производства",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _seed_order(
    db_session,
    account: MarketplaceAccount,
    product: Product,
    *,
    code: str,
    ordered_at: datetime,
    quantity: int = 1,
) -> tuple[MarketplaceOrder, MarketplaceOrderLine]:
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=code,
        external_code=code,
        status="accepted",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("1000") * quantity,
        ordered_at=ordered_at,
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id=f"line-{code}",
        product_id=product.id,
        external_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        title=product.name,
        quantity=quantity,
        unit_price=Decimal("1000"),
        line_total=Decimal("1000") * quantity,
    )
    order.lines.append(line)
    db_session.add(order)
    db_session.flush()
    return order, line


def _seed_context(db_session):
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    product = _seed_product(db_session)
    first_order, first_line = _seed_order(
        db_session,
        account,
        product,
        code="1000000001",
        ordered_at=NOW,
    )
    second_order, second_line = _seed_order(
        db_session,
        account,
        product,
        code="1000000002",
        ordered_at=NOW + timedelta(minutes=5),
    )
    batch, allocated = create_inventory_batch(
        db_session,
        product=product,
        quantity=100,
        unit_cost=Decimal("250"),
        received_at=NOW + timedelta(days=1),
        source_name="Производство",
        is_received=False,
        batch_type=InventoryBatchType.PRODUCTION,
    )
    return (
        product,
        batch,
        first_order,
        first_line,
        second_order,
        second_line,
        allocated,
    )


def test_production_capacity_covers_orders_without_moving_them_to_packaging(
    db_session,
) -> None:
    product, batch, first_order, first_line, second_order, second_line, allocated = (
        _seed_context(db_session)
    )

    assert allocated == 0
    assert batch.quantity_remaining == 0
    assert db_session.scalars(select(InventoryAllocation)).all() == []

    reservations = build_incoming_reservations(
        db_session,
        product_ids={product.id},
    )
    assert [
        (row.batch_id, row.order_line_id, row.reserved_quantity)
        for row in reservations
    ] == [
        (batch.id, first_line.id, 1),
        (batch.id, second_line.id, 1),
    ]

    _total, orders = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=20,
        offset=0,
    )
    stages = {order.order_id: order.stage for order in orders}
    assert stages[first_order.id] is CommerceOrderStage.PREORDER
    assert stages[second_order.id] is CommerceOrderStage.PREORDER

    snapshot = _product_inventory(db_session, product.id)
    production = next(row for row in snapshot.batches if row.id == batch.id)
    assert snapshot.on_hand == 0
    assert snapshot.production_planned_total == 100
    assert snapshot.production_completed_total == 0
    assert production.quantity_remaining == 100
    assert [row.external_code for row in production.production_orders] == [
        "1000000001",
        "1000000002",
    ]


def test_manufactured_action_allocates_only_selected_active_preorder(
    db_session,
) -> None:
    product, batch, first_order, first_line, second_order, second_line, _allocated = (
        _seed_context(db_session)
    )

    completed = complete_production_order(
        db_session,
        batch=batch,
        order_line=first_line,
        completed_at=NOW + timedelta(hours=2),
    )

    assert completed.completed_quantity == 1
    assert completed.order_line_fully_allocated is True
    allocation = db_session.scalar(
        select(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id == batch.id,
            InventoryAllocation.marketplace_order_line_id == first_line.id,
        )
    )
    assert allocation is not None
    assert allocation.quantity == 1
    assert Decimal(allocation.unit_cost) == Decimal("250")

    _total, orders = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=20,
        offset=0,
    )
    by_id = {order.order_id: order for order in orders}
    assert by_id[first_order.id].stage is CommerceOrderStage.PREORDER
    assert by_id[first_order.id].lines[0].procurement_source_name == "Производство"
    assert by_id[second_order.id].stage is CommerceOrderStage.PREORDER

    snapshot = _product_inventory(db_session, product.id)
    production = next(row for row in snapshot.batches if row.id == batch.id)
    assert snapshot.on_hand == 0
    assert snapshot.production_completed_total == 1
    assert production.quantity_allocated == 1
    assert production.quantity_remaining == 99
    assert [row.order_line_id for row in production.production_orders] == [
        second_line.id
    ]


def test_fifo_rebuild_preserves_confirmed_production_completion(db_session) -> None:
    product, batch, _first_order, first_line, _second_order, _second_line, _allocated = (
        _seed_context(db_session)
    )
    complete_production_order(
        db_session,
        batch=batch,
        order_line=first_line,
        completed_at=NOW + timedelta(hours=2),
    )

    rebuild_product_fifo(db_session, product_id=product.id)

    allocation = db_session.scalar(
        select(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id == batch.id,
            InventoryAllocation.marketplace_order_line_id == first_line.id,
        )
    )
    assert allocation is not None
    assert allocation.quantity == 1


def test_deleting_purchase_batch_does_not_erase_manufactured_orders(
    db_session,
) -> None:
    product, production_batch, _first_order, first_line, *_rest = _seed_context(
        db_session
    )
    complete_production_order(
        db_session,
        batch=production_batch,
        order_line=first_line,
        completed_at=NOW + timedelta(hours=2),
    )
    purchase_batch, _allocated = create_inventory_batch(
        db_session,
        product=product,
        quantity=3,
        unit_cost=Decimal("300"),
        received_at=NOW - timedelta(days=1),
        reconcile_existing_orders=False,
    )

    response = delete_product_inventory_batch(
        product.id,
        purchase_batch.id,
        db_session,
    )

    assert response.status_code == 204
    allocation = db_session.scalar(
        select(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id == production_batch.id,
            InventoryAllocation.marketplace_order_line_id == first_line.id,
        )
    )
    assert allocation is not None
    assert allocation.quantity == 1


def test_manufactured_action_is_idempotent(db_session) -> None:
    _product, batch, _first_order, first_line, *_rest = _seed_context(db_session)

    first = complete_production_order(
        db_session,
        batch=batch,
        order_line=first_line,
        completed_at=NOW + timedelta(hours=2),
    )
    repeated = complete_production_order(
        db_session,
        batch=batch,
        order_line=first_line,
        completed_at=NOW + timedelta(hours=2, minutes=1),
    )

    assert first.completed_quantity == 1
    assert repeated.completed_quantity == 0
    allocation = db_session.scalar(
        select(InventoryAllocation).where(
            InventoryAllocation.inventory_batch_id == batch.id,
            InventoryAllocation.marketplace_order_line_id == first_line.id,
        )
    )
    assert allocation is not None
    assert allocation.quantity == 1


def test_manufactured_signal_resolves_procurement_without_renaming_preorder() -> None:
    line = CommerceOrderLine(
        line_id=1,
        product_id=1,
        external_product_id="PRODUCTION-1",
        merchant_sku="PRODUCTION-1",
        title="Товар собственного производства",
        quantity=1,
        unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
        purchase_request_id="stale-purchase",
        purchase_status="ordered",
        inventory_allocated_quantity=1,
        production_completed_quantity=1,
    )
    order = CommerceOrder(
        order_id=1,
        external_code="1000000001",
        marketplace="kaspi",
        status="accepted",
        currency="KZT",
        total_amount=Decimal("1000"),
        ordered_at=NOW,
        delivered_at=None,
        lines=(line,),
    )

    assert order.stage is CommerceOrderStage.PREORDER
    assert order.effective_procurement_state(line) is ProcurementState.NOT_REQUIRED


def test_production_batch_cannot_be_received_as_whole_stock(db_session) -> None:
    _product, batch, *_rest = _seed_context(db_session)

    with pytest.raises(ValueError, match="per order"):
        mark_inventory_batch_received(
            db_session,
            batch=batch,
            received_at=NOW + timedelta(days=1),
        )
