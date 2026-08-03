from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app.commerce.api import OrderStageOverrideRequest, override_order_stage
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import allocate_order_line_fifo, rebuild_product_fifo
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderLine,
    Product,
)


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _order_fixture(db_session, *, with_stock: bool) -> tuple[MarketplaceOrder, MarketplaceOrderLine, InventoryBatch | None]:
    product = Product(
        kaspi_product_id="STAGE-PRODUCT",
        merchant_sku="STAGE-PRODUCT_SKU",
        name="Stage product",
        status="active",
    )
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id=f"stage-account-{int(with_stock)}",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add_all([product, account])
    db_session.flush()
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=f"stage-order-{int(with_stock)}",
        external_code=f"stage-order-{int(with_stock)}",
        status="assembly",
        original_status="ASSEMBLY",
        currency="KZT",
        total_amount=Decimal("5000"),
        ordered_at=NOW,
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id="stage-line",
        product_id=product.id,
        external_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        title=product.name,
        quantity=1,
        unit_price=Decimal("5000"),
        line_total=Decimal("5000"),
    )
    order.lines.append(line)
    account.orders.append(order)
    batch = None
    if with_stock:
        batch = InventoryBatch(
            product_id=product.id,
            received_at=NOW,
            quantity_received=1,
            quantity_remaining=1,
            unit_cost=Decimal("2000"),
            is_received=True,
        )
        db_session.add(batch)
    db_session.flush()
    return order, line, batch


def test_manual_packaging_is_rejected_when_fifo_does_not_cover_order(db_session) -> None:
    order, _line, _batch = _order_fixture(db_session, with_stock=False)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        override_order_stage(
            order.id,
            OrderStageOverrideRequest(
                stage="assembly",
                reason="Проверка ошибочной упаковки",
            ),
            db_session,
        )

    assert exc_info.value.status_code == 409
    stored = db_session.get(MarketplaceOrder, order.id)
    assert stored is not None
    assert stored.manual_stage is None
    assert db_session.scalar(select(func.count(MarketplaceOrderEvent.id))) == 0


def test_manual_cancellation_releases_fifo_and_blocks_reallocation(db_session) -> None:
    order, line, batch = _order_fixture(db_session, with_stock=True)
    assert batch is not None
    allocate_order_line_fifo(db_session, order_line=line, order=order)
    assert batch.quantity_remaining == 0
    db_session.commit()

    result = override_order_stage(
        order.id,
        OrderStageOverrideRequest(
            stage="cancelled",
            reason="Заказ ошибочно попал в упаковку",
        ),
        db_session,
    )

    assert result["manual_stage"] == "cancelled"
    assert batch.quantity_remaining == 1
    assert db_session.scalar(select(func.count(InventoryAllocation.id))) == 0
    assert rebuild_product_fifo(db_session, product_id=line.product_id or 0) == 0
    assert batch.quantity_remaining == 1
    events = db_session.scalars(
        select(MarketplaceOrderEvent).order_by(MarketplaceOrderEvent.id)
    ).all()
    assert [event.event_type for event in events] == [
        "inventory_released",
        "manual_stage_changed",
    ]


def test_manual_preorder_audits_reason_and_keeps_source_status(db_session) -> None:
    order, _line, _batch = _order_fixture(db_session, with_stock=False)

    result = override_order_stage(
        order.id,
        OrderStageOverrideRequest(
            stage="preorder",
            reason="Недостаточно подтверждённых партий",
        ),
        db_session,
    )

    assert result["manual_stage"] == "preorder"
    assert result["source_status"] == "assembly"
    assert order.manual_stage_reason == "Недостаточно подтверждённых партий"
    event = db_session.scalar(
        select(MarketplaceOrderEvent).where(
            MarketplaceOrderEvent.event_type == "manual_stage_changed"
        )
    )
    assert event is not None
    assert event.metadata_json["source_status"] == "assembly"


def test_manual_override_cannot_pull_back_kaspi_shipping_stage(db_session) -> None:
    order, _line, _batch = _order_fixture(db_session, with_stock=False)
    order.status = "shipping"
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        override_order_stage(
            order.id,
            OrderStageOverrideRequest(
                stage="preorder",
                reason="Ошибочная попытка вернуть назад",
            ),
            db_session,
        )

    assert exc_info.value.status_code == 409
    assert order.manual_stage is None
