from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app import inventory_service
from backend.app.commerce.domain import CommerceOrderStage
from backend.app.commerce.repository import SqlAlchemyCommerceRepository
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import create_inventory_batch, mark_inventory_batch_received
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product


NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def _product(db_session, code: str) -> Product:
    product = Product(
        kaspi_product_id=code,
        merchant_sku=code,
        name=f"Product {code}",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _line(
    db_session,
    account: MarketplaceAccount,
    product: Product,
    *,
    number: int,
    quantity: int = 1,
    status: str = "preorder",
) -> MarketplaceOrderLine:
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=str(number),
        external_code=str(number),
        status=status,
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("1000") * quantity,
        ordered_at=NOW + timedelta(minutes=number),
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id=f"line-{number}",
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
    return line


def _incoming(db_session, product: Product, quantity: int) -> InventoryBatch:
    batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW + timedelta(days=1),
        quantity_received=quantity,
        quantity_remaining=0,
        unit_cost=Decimal("500"),
        is_received=False,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def test_incoming_stock_is_reserved_by_order_fifo_and_product(db_session) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    first_product = _product(db_session, "P-1")
    second_product = _product(db_session, "P-2")
    first_product_lines = [
        _line(db_session, account, first_product, number=index)
        for index in range(1, 10)
    ]
    second_product_line = _line(
        db_session, account, second_product, number=20, quantity=2
    )
    cancelled_line = _line(
        db_session, account, first_product, number=0, status="cancelled"
    )
    _incoming(db_session, first_product, 5)
    _incoming(db_session, second_product, 1)

    reservations = SqlAlchemyCommerceRepository(db_session)._incoming_reservations()

    assert [reservations.get(line.id, 0) for line in first_product_lines] == [
        1, 1, 1, 1, 1, 0, 0, 0, 0
    ]
    assert reservations[second_product_line.id] == 1
    assert cancelled_line.id not in reservations


def test_physical_fifo_is_deducted_before_incoming_reservation(db_session) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    product = _product(db_session, "P-3")
    first = _line(db_session, account, product, number=1, quantity=3)
    second = _line(db_session, account, product, number=2, quantity=3)
    physical_batch = InventoryBatch(
        product_id=product.id,
        received_at=NOW,
        quantity_received=2,
        quantity_remaining=0,
        unit_cost=Decimal("400"),
        is_received=True,
    )
    db_session.add(physical_batch)
    db_session.flush()
    db_session.add(
        InventoryAllocation(
            inventory_batch_id=physical_batch.id,
            marketplace_order_line_id=first.id,
            quantity=2,
            unit_cost=Decimal("400"),
            allocated_at=NOW,
        )
    )
    _incoming(db_session, product, 2)
    db_session.flush()

    reservations = SqlAlchemyCommerceRepository(db_session)._incoming_reservations()

    assert reservations[first.id] == 1
    assert reservations[second.id] == 1


def test_received_batches_move_preorders_to_packaging_in_order_fifo(
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        inventory_service,
        "_sync_product_inventory_to_feed",
        lambda *_args, **_kwargs: None,
    )
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    product = _product(db_session, "PENTA-1")
    first = _line(db_session, account, product, number=1, status="accepted")
    second = _line(db_session, account, product, number=2, status="accepted")
    incoming_batch, allocated = create_inventory_batch(
        db_session,
        product=product,
        quantity=1,
        unit_cost=Decimal("589"),
        received_at=NOW + timedelta(days=1),
        source_name="FIFO supplier",
        is_received=False,
    )

    assert allocated == 0
    _total, before_receipt = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=20,
        offset=0,
    )
    assert {order.stage for order in before_receipt} == {
        CommerceOrderStage.PREORDER
    }

    assert mark_inventory_batch_received(
        db_session,
        batch=incoming_batch,
        received_at=NOW + timedelta(hours=1),
    ) == 1
    _total, after_first_receipt = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=20,
        offset=0,
    )
    stages_by_line = {
        order.lines[0].line_id: order.stage for order in after_first_receipt
    }
    assert stages_by_line[first.id] is CommerceOrderStage.ASSEMBLY
    assert stages_by_line[second.id] is CommerceOrderStage.PREORDER

    _second_batch, allocated = create_inventory_batch(
        db_session,
        product=product,
        quantity=1,
        unit_cost=Decimal("589"),
        received_at=NOW + timedelta(hours=2),
        source_name="FIFO supplier",
    )
    assert allocated == 1
    _total, after_second_receipt = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=20,
        offset=0,
    )
    assert {order.stage for order in after_second_receipt} == {
        CommerceOrderStage.ASSEMBLY
    }
