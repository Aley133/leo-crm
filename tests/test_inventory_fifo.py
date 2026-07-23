from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import allocate_order_line_fifo, create_inventory_batch
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product


def _product(db_session) -> Product:
    product = Product(
        kaspi_product_id="105721344",
        merchant_sku="105721344",
        name="Test product",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _order_line(db_session, product: Product, *, code: str, ordered_at: datetime, quantity: int = 1):
    account = db_session.scalar(select(MarketplaceAccount).limit(1))
    if account is None:
        account = MarketplaceAccount(
            provider="kaspi",
            external_account_id="11843018",
            display_name="Kaspi",
            timezone="Asia/Almaty",
        )
        db_session.add(account)
        db_session.flush()
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=code,
        external_code=code,
        status="accepted",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("1499") * quantity,
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
        unit_price=Decimal("1499"),
        line_total=Decimal("1499") * quantity,
    )
    order.lines.append(line)
    db_session.add(order)
    db_session.flush()
    return order, line


def test_fifo_allocates_oldest_batch_first_and_is_idempotent(db_session) -> None:
    product = _product(db_session)
    first, _ = create_inventory_batch(
        db_session,
        product=product,
        quantity=1,
        unit_cost=Decimal("500"),
        received_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        reconcile_existing_orders=False,
    )
    second, _ = create_inventory_batch(
        db_session,
        product=product,
        quantity=2,
        unit_cost=Decimal("600"),
        received_at=datetime(2026, 7, 23, 9, 0, tzinfo=UTC),
        reconcile_existing_orders=False,
    )
    order, line = _order_line(
        db_session,
        product,
        code="1001",
        ordered_at=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
        quantity=2,
    )

    result = allocate_order_line_fifo(db_session, order_line=line, order=order)
    repeated = allocate_order_line_fifo(db_session, order_line=line, order=order)

    assert result.newly_allocated_quantity == 2
    assert result.fully_allocated is True
    assert repeated.newly_allocated_quantity == 0
    assert first.quantity_remaining == 0
    assert second.quantity_remaining == 1
    allocations = db_session.scalars(
        select(InventoryAllocation).order_by(InventoryAllocation.inventory_batch_id)
    ).all()
    assert [(row.inventory_batch_id, row.quantity, Decimal(row.unit_cost)) for row in allocations] == [
        (first.id, 1, Decimal("500")),
        (second.id, 1, Decimal("600")),
    ]


def test_new_batch_reconciles_active_orders_from_receipt_day(db_session) -> None:
    product = _product(db_session)
    _order, line = _order_line(
        db_session,
        product,
        code="1002",
        ordered_at=datetime(2026, 7, 23, 3, 0, tzinfo=UTC),
        quantity=1,
    )

    batch, allocated = create_inventory_batch(
        db_session,
        product=product,
        quantity=10,
        unit_cost=Decimal("700"),
        received_at=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
        reconcile_existing_orders=True,
    )

    assert allocated == 1
    assert batch.quantity_remaining == 9
    allocation = db_session.scalar(
        select(InventoryAllocation).where(
            InventoryAllocation.marketplace_order_line_id == line.id
        )
    )
    assert allocation is not None
    assert allocation.quantity == 1
    assert Decimal(allocation.unit_cost) == Decimal("700")


def test_cancelled_order_does_not_consume_stock(db_session) -> None:
    product = _product(db_session)
    batch, _ = create_inventory_batch(
        db_session,
        product=product,
        quantity=3,
        unit_cost=Decimal("800"),
        received_at=datetime(2026, 7, 23, 8, 0, tzinfo=UTC),
        reconcile_existing_orders=False,
    )
    order, line = _order_line(
        db_session,
        product,
        code="1003",
        ordered_at=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
    )
    order.status = "cancelled"

    result = allocate_order_line_fifo(db_session, order_line=line, order=order)

    assert result.newly_allocated_quantity == 0
    assert batch.quantity_remaining == 3
