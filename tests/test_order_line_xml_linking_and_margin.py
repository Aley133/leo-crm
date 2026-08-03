from decimal import Decimal

from backend.app.commerce.domain import CommerceOrderLine
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product
from backend.app.order_line_product_linking import (
    find_product_for_order_line,
    link_all_matching_order_lines,
)


def test_xml_product_links_existing_order_line_by_merchant_sku(db_session) -> None:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="11843018",
        display_name="Kaspi",
    )
    db_session.add(account)
    db_session.flush()
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id="order-1",
        external_code="1007942113",
        status="accepted",
        original_status="ACCEPTED_BY_MERCHANT",
        currency="KZT",
        total_amount=Decimal("1499"),
    )
    db_session.add(order)
    db_session.flush()
    line = MarketplaceOrderLine(
        marketplace_order_id=order.id,
        external_line_id="entry-1",
        external_product_id="MTY5NjQ1NTM4",
        merchant_sku="854792406",
        title="Unknown product",
        quantity=1,
        unit_price=Decimal("1499"),
        line_total=Decimal("1499"),
    )
    product = Product(
        kaspi_product_id="854792406",
        merchant_sku="854792406",
        name="Органайзер пластик",
        status="active",
    )
    db_session.add_all([line, product])
    db_session.flush()

    assert link_all_matching_order_lines(db_session, product=product) == 1
    assert line.product_id == product.id
    assert line.title == "Органайзер пластик"


def test_numeric_master_product_id_resolves_unique_compound_seller_sku(
    db_session,
) -> None:
    product = Product(
        kaspi_product_id="112568945_794773202",
        merchant_sku="112568945_794773202",
        name="GLS Pharmaceuticals Мужская формула",
        status="active",
    )
    line = MarketplaceOrderLine(
        external_line_id="entry-new-order",
        external_product_id="112568945",
        merchant_sku=None,
        title="Unknown product",
        quantity=1,
        unit_price=Decimal("3585"),
        line_total=Decimal("3585"),
    )
    db_session.add(product)
    db_session.flush()

    resolved = find_product_for_order_line(db_session, line)

    assert resolved is not None
    assert resolved.id == product.id


def test_numeric_master_product_id_never_guesses_between_compound_skus(
    db_session,
) -> None:
    db_session.add_all(
        [
            Product(
                kaspi_product_id="112568945_111111111",
                merchant_sku="112568945_111111111",
                name="Первый оффер",
                status="active",
            ),
            Product(
                kaspi_product_id="112568945_222222222",
                merchant_sku="112568945_222222222",
                name="Второй оффер",
                status="active",
            ),
        ]
    )
    db_session.flush()
    line = MarketplaceOrderLine(
        external_line_id="entry-ambiguous",
        external_product_id="112568945",
        merchant_sku=None,
        title="Unknown product",
        quantity=1,
        unit_price=Decimal("3585"),
        line_total=Decimal("3585"),
    )

    assert find_product_for_order_line(db_session, line) is None


def test_order_line_margin_uses_procurement_source_price() -> None:
    line = CommerceOrderLine(
        line_id=1,
        product_id=10,
        external_product_id="854792406",
        merchant_sku="854792406",
        title="Органайзер пластик",
        quantity=2,
        unit_price=Decimal("1499"),
        line_total=Decimal("2998"),
        purchase_request_id=None,
        purchase_status=None,
        procurement_unit_cost=Decimal("700"),
        procurement_source_name="Собственное производство",
    )

    assert line.procurement_total_cost == Decimal("1400")
    assert line.gross_margin == Decimal("1598")
    assert line.gross_margin_pct == Decimal("53.30")
