from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.app.commerce.domain import CommerceOrder, CommerceOrderLine
from backend.app.commerce.service import CommerceService


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
        query: str | None = None,
    ) -> tuple[int, tuple[CommerceOrder, ...]]:
        self.calls.append(
            {
                "limit": limit,
                "offset": offset,
                "status": status,
                "query": query,
            }
        )
        rows = self.orders
        if status is not None:
            rows = tuple(order for order in rows if order.status == status)
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


def test_kaspi_preorder_filter_keeps_covered_and_uncovered_preorders() -> None:
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

    assert total == 40
    assert len(visible) == 40
    assert summary.orders_count == 40


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
