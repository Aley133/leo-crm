from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.app.commerce.repository import SqlAlchemyCommerceRepository
from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine
from backend.app.commerce.service import CommerceService
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    Product,
)


ROOT = Path(__file__).resolve().parents[1]


class InMemoryCommerceRepository:
    def __init__(self, orders: tuple[CommerceOrder, ...]) -> None:
        self.orders = orders
        self.calls: list[dict[str, object]] = []

    def list_orders(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        operational_stage: str | None = None,
        query: str | None = None,
    ) -> tuple[int, tuple[CommerceOrder, ...]]:
        self.calls.append(
            {
                "limit": limit,
                "offset": offset,
                "status": status,
                "operational_stage": operational_stage,
                "query": query,
            }
        )
        rows = self.orders
        if status is not None:
            rows = tuple(order for order in rows if order.status == status)
        if operational_stage is not None:
            rows = tuple(
                order
                for order in rows
                if order.stage.value == operational_stage
            )
        return len(rows), rows[offset : offset + limit]


def _order(order_id: int, *, fifo_ready: bool, status: str = "assembly") -> CommerceOrder:
    line = CommerceOrderLine(
        line_id=order_id,
        product_id=order_id,
        external_product_id=f"kaspi-{order_id}",
        merchant_sku=f"sku-{order_id}",
        title=f"Товар {order_id}",
        quantity=1,
        unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
        purchase_request_id=None,
        purchase_status=None,
        inventory_allocated_quantity=1 if fifo_ready else 0,
    )
    return CommerceOrder(
        order_id=order_id,
        external_code=str(1020000000 + order_id),
        marketplace="kaspi",
        marketplace_account_id=2,
        marketplace_external_account_id="30295031",
        status=status,
        original_status=("ACCEPTED_BY_MERCHANT" if status == "accepted" else "ASSEMBLY"),
        currency="KZT",
        total_amount=Decimal("1000"),
        ordered_at=datetime(2026, 8, 4, tzinfo=UTC),
        delivered_at=None,
        lines=(line,),
    )


def test_kaspi_packaging_filter_keeps_all_orders_regardless_of_fifo() -> None:
    orders = tuple(_order(index, fifo_ready=index <= 16) for index in range(1, 57))
    repository = InMemoryCommerceRepository(orders)
    service = CommerceService(repository)

    total, visible, summary = service.list_orders(
        limit=200,
        offset=0,
        status="assembly",
    )

    assert total == 56
    assert len(visible) == 56
    assert summary.orders_count == 56
    assert repository.calls[-1]["status"] is None
    assert repository.calls[-1]["operational_stage"] == "assembly"
    assert repository.calls[-1]["limit"] == 200


def test_preorder_filter_keeps_only_orders_without_received_fifo_coverage() -> None:
    orders = tuple(
        _order(index, fifo_ready=index <= 16, status="accepted")
        for index in range(1, 41)
    )
    service = CommerceService(InMemoryCommerceRepository(orders))

    total, visible, summary = service.list_orders(
        limit=200,
        offset=0,
        status="preorder",
    )

    assert total == 24
    assert len(visible) == 24
    assert summary.orders_count == 24


def test_packaging_filter_adds_fifo_covered_preorders() -> None:
    orders = tuple(
        _order(index, fifo_ready=index <= 16, status="accepted")
        for index in range(1, 41)
    )
    service = CommerceService(InMemoryCommerceRepository(orders))

    total, visible, summary = service.list_orders(
        limit=200,
        offset=0,
        status="assembly",
    )

    assert total == 16
    assert len(visible) == 16
    assert summary.orders_count == 16


def test_orders_ui_exposes_one_authoritative_kaspi_filter() -> None:
    html = (ROOT / "backend" / "app" / "static" / "orders.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(
        encoding="utf-8"
    )

    assert html.count('id="status"') == 1
    assert 'id="kaspi-status"' not in html
    assert "Статус Kaspi" in html
    assert "Этап LEO" not in html
    assert 'params.set("kaspi_status", kaspiStatus)' not in script
    assert "Kaspi + фактическое покрытие FIFO" not in script
    assert "<span>Статус Kaspi</span>" in script


def _database_order(
    db_session,
    account: MarketplaceAccount,
    product: Product,
    *,
    number: int,
    status: str,
    allocated: bool = False,
    manual_stage: str | None = None,
) -> MarketplaceOrder:
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=f"FILTER-{number}",
        external_code=f"FILTER-{number}",
        status=status,
        original_status="ACCEPTED_BY_MERCHANT",
        manual_stage=manual_stage,
        currency="KZT",
        total_amount=Decimal("1000"),
        ordered_at=datetime(2026, 8, 4, 10, number, tzinfo=UTC),
    )
    line = MarketplaceOrderLine(
        external_line_id=f"FILTER-LINE-{number}",
        product_id=product.id,
        external_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        title=product.name,
        quantity=1,
        unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
    )
    order.lines.append(line)
    db_session.add(order)
    db_session.flush()
    if allocated:
        batch = InventoryBatch(
            product_id=product.id,
            received_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            quantity_received=1,
            quantity_remaining=0,
            unit_cost=Decimal("500"),
            source_name="Filter test FIFO",
        )
        db_session.add(batch)
        db_session.flush()
        db_session.add(
            InventoryAllocation(
                inventory_batch_id=batch.id,
                marketplace_order_line_id=line.id,
                quantity=1,
                unit_cost=Decimal("500"),
                allocated_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
            )
        )
        db_session.flush()
    return order


def test_sql_stage_filter_matches_domain_and_paginates_before_enrichment(
    db_session,
) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="filter-account",
        display_name="Filter account",
        timezone="Asia/Almaty",
    )
    product = Product(
        kaspi_product_id="FILTER-PRODUCT",
        merchant_sku="FILTER-PRODUCT",
        name="Filter product",
        status="active",
    )
    db_session.add_all((account, product))
    db_session.flush()
    preorder = _database_order(
        db_session,
        account,
        product,
        number=1,
        status="accepted",
    )
    covered = _database_order(
        db_session,
        account,
        product,
        number=2,
        status="accepted",
        allocated=True,
    )
    native_packaging = _database_order(
        db_session,
        account,
        product,
        number=3,
        status="assembly",
    )
    handover = _database_order(
        db_session,
        account,
        product,
        number=4,
        status="handover",
        manual_stage="preorder",
    )
    manual_preorder = _database_order(
        db_session,
        account,
        product,
        number=5,
        status="new",
        manual_stage="preorder",
    )

    repository = SqlAlchemyCommerceRepository(db_session)
    total, first_page = repository.list_orders(
        limit=1,
        offset=0,
        operational_stage="assembly",
    )
    preorder_total, preorder_rows = repository.list_orders(
        limit=20,
        offset=0,
        operational_stage="preorder",
    )
    handover_total, handover_rows = repository.list_orders(
        limit=20,
        offset=0,
        operational_stage="handover",
    )

    assert total == 2
    assert len(first_page) == 1
    assert first_page[0].order_id == native_packaging.id
    assert preorder_total == 2
    assert {row.order_id for row in preorder_rows} == {
        preorder.id,
        manual_preorder.id,
    }
    assert handover_total == 1
    assert [row.order_id for row in handover_rows] == [handover.id]
    assert covered.id != first_page[0].order_id


def test_orders_page_limits_incoming_fifo_scan_to_visible_products(
    db_session,
    monkeypatch,
) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="visible-products-account",
        display_name="Visible products account",
        timezone="Asia/Almaty",
    )
    first = Product(
        kaspi_product_id="VISIBLE-FIRST",
        merchant_sku="VISIBLE-FIRST",
        name="Visible first",
        status="active",
    )
    second = Product(
        kaspi_product_id="VISIBLE-SECOND",
        merchant_sku="VISIBLE-SECOND",
        name="Visible second",
        status="active",
    )
    db_session.add_all((account, first, second))
    db_session.flush()
    _database_order(
        db_session,
        account,
        first,
        number=1,
        status="preorder",
    )
    newest = _database_order(
        db_session,
        account,
        second,
        number=2,
        status="preorder",
    )
    observed: list[set[int] | None] = []

    def reservations(_session, *, product_ids=None):
        observed.append(product_ids)
        return ()

    monkeypatch.setattr(
        "backend.app.commerce.repository.build_incoming_reservations",
        reservations,
    )

    _total, rows = SqlAlchemyCommerceRepository(db_session).list_orders(
        limit=1,
        offset=0,
    )

    assert [row.order_id for row in rows] == [newest.id]
    assert observed == [{second.id}]
