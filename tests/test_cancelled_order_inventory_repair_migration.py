import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import allocate_order_line_fifo
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    Product,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260731_0027_restore_cancelled_order_inventory.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "cancelled_order_inventory_repair",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_restores_existing_cancelled_order_allocation_once(
    db_session,
) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="repair-account",
        display_name="Kaspi",
    )
    product = Product(
        kaspi_product_id="repair-product",
        merchant_sku="repair-product",
        name="Repair product",
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    batch = InventoryBatch(
        product_id=product.id,
        received_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("4500"),
        source_name="OZON",
    )
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id="repair-order",
        external_code="1009001001",
        status="accepted",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("9000"),
        ordered_at=datetime(2026, 7, 30, 11, 0, tzinfo=UTC),
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id="repair-line",
        product_id=product.id,
        external_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        title=product.name,
        quantity=1,
        unit_price=Decimal("9000"),
        line_total=Decimal("9000"),
    )
    order.lines.append(line)
    db_session.add_all([batch, order])
    db_session.flush()
    allocate_order_line_fifo(db_session, order_line=line, order=order)
    order.status = "cancelled"
    db_session.commit()

    migration = _load_migration()
    first = migration._restore_cancelled_order_allocations(db_session.connection())
    second = migration._restore_cancelled_order_allocations(db_session.connection())
    db_session.expire_all()

    assert first == 1
    assert second == 0
    assert db_session.get(InventoryBatch, batch.id).quantity_remaining == 2
    assert db_session.scalars(select(InventoryAllocation)).all() == []
