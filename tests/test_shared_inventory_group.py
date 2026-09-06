from datetime import UTC, datetime, timedelta
from decimal import Decimal
from xml.etree import ElementTree

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.dumping_models import DumpingPolicy, KaspiXmlFeed
from backend.app.inventory_api import (
    InventoryOwnerUpdate,
    detach_product_inventory,
    get_product_inventory,
    merge_product_inventory,
)
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.inventory_service import (
    build_incoming_reservations,
    rebuild_product_fifo,
)
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    Product,
)


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _product(db_session, code: str, *, workspace_id: int = 1) -> Product:
    product = Product(
        workspace_id=workspace_id,
        kaspi_product_id=code,
        merchant_sku=f"{code}_SKU",
        name=f"Product {code}",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def _order_line(
    db_session,
    account: MarketplaceAccount,
    product: Product,
    *,
    code: str,
    ordered_at: datetime,
    status: str = "assembly",
) -> MarketplaceOrderLine:
    order = MarketplaceOrder(
        marketplace_account_id=account.id,
        external_order_id=code,
        external_code=code,
        status=status,
        original_status="ASSEMBLY",
        currency="KZT",
        total_amount=Decimal("5000"),
        ordered_at=ordered_at,
        version=1,
    )
    line = MarketplaceOrderLine(
        external_line_id=f"line-{code}",
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
    db_session.flush()
    return line


def _offer_stock(xml_text: str, sku: str) -> tuple[str, str]:
    root = ElementTree.fromstring(xml_text)
    offer = next(node for node in root.iter("offer") if node.attrib["sku"] == sku)
    availability = next(offer.iter("availability"))
    return availability.attrib["available"], availability.attrib["stockCount"]


def test_shared_skus_consume_one_fifo_pool_by_order_time_and_sync_both_offers(
    db_session,
) -> None:
    owner = _product(db_session, "OWNER")
    variant = _product(db_session, "VARIANT")
    variant.inventory_owner_product_id = owner.id
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="shared-stock-account",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    xml = f"""<kaspi_catalog><offers>
      <offer sku='{owner.merchant_sku}'><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='1'/></offer>
      <offer sku='{variant.merchant_sku}'><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='1'/></offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        merchant_id="shared-stock-account",
        source_filename="catalog.xml",
        source_xml=xml,
        generated_xml=xml,
        active=True,
    )
    batch = InventoryBatch(
        product_id=owner.id,
        received_at=NOW,
        quantity_received=1,
        quantity_remaining=1,
        unit_cost=Decimal("2000"),
        is_received=True,
    )
    db_session.add_all(
        [
            feed,
            batch,
            DumpingPolicy(
                product_id=owner.id,
                enabled=True,
                auto_publish_xml=True,
            ),
            DumpingPolicy(
                product_id=variant.id,
                enabled=True,
                auto_publish_xml=True,
            ),
        ]
    )
    db_session.flush()

    later_owner_line = _order_line(
        db_session,
        account,
        owner,
        code="later-owner",
        ordered_at=NOW + timedelta(minutes=2),
    )
    earlier_variant_line = _order_line(
        db_session,
        account,
        variant,
        code="earlier-variant",
        ordered_at=NOW + timedelta(minutes=1),
    )

    allocated = rebuild_product_fifo(db_session, product_id=variant.id)

    assert allocated == 1
    assert batch.quantity_remaining == 0
    allocations = db_session.scalars(select(InventoryAllocation)).all()
    assert [(row.marketplace_order_line_id, row.quantity) for row in allocations] == [
        (earlier_variant_line.id, 1)
    ]
    assert later_owner_line.id not in {
        row.marketplace_order_line_id for row in allocations
    }
    assert _offer_stock(feed.generated_xml, owner.merchant_sku or "") == ("no", "0")
    assert _offer_stock(feed.generated_xml, variant.merchant_sku or "") == ("no", "0")


def test_expected_stock_reserves_assembly_orders_across_shared_skus(db_session) -> None:
    owner = _product(db_session, "INCOMING-OWNER")
    variant = _product(db_session, "INCOMING-VARIANT")
    variant.inventory_owner_product_id = owner.id
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="shared-incoming-account",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    line = _order_line(
        db_session,
        account,
        variant,
        code="incoming-variant",
        ordered_at=NOW,
        status="assembly",
    )
    batch = InventoryBatch(
        product_id=owner.id,
        received_at=NOW + timedelta(days=1),
        quantity_received=1,
        quantity_remaining=0,
        unit_cost=Decimal("2200"),
        is_received=False,
    )
    db_session.add(batch)
    db_session.flush()

    reservations = build_incoming_reservations(
        db_session,
        product_ids={variant.id},
    )

    assert [(row.batch_id, row.order_line_id, row.reserved_quantity) for row in reservations] == [
        (batch.id, line.id, 1)
    ]


def test_inventory_groups_cannot_cross_workspaces(db_session) -> None:
    first = _product(db_session, "FIRST", workspace_id=1)
    db_session.info["include_all_workspaces"] = True
    try:
        foreign = _product(db_session, "FOREIGN", workspace_id=2)
        db_session.commit()
    finally:
        db_session.info.pop("include_all_workspaces", None)

    with pytest.raises(HTTPException) as exc_info:
        merge_product_inventory(
            first.id,
            InventoryOwnerUpdate(owner_product_id=foreign.id),
            db_session,
        )

    # Depending on whether SQLAlchemy can reuse the identity-map row, the
    # workspace guard either hides the foreign product or reaches the explicit
    # account mismatch check. Both paths reject the merge before any write.
    assert exc_info.value.status_code in {404, 409}
    assert first.inventory_owner_product_id is None


def test_shared_inventory_member_can_detach_and_relink_without_moving_owner_batches(
    db_session,
) -> None:
    owner = _product(db_session, "OWNER-DETACH")
    wrong_member = _product(db_session, "WRONG-MEMBER")
    correct_owner = _product(db_session, "CORRECT-OWNER")
    wrong_member.inventory_owner_product_id = owner.id
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id="shared-detach-account",
        display_name="Kaspi",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    owner_line = _order_line(
        db_session,
        account,
        owner,
        code="owner-active-order",
        ordered_at=NOW,
    )
    wrong_line = _order_line(
        db_session,
        account,
        wrong_member,
        code="wrong-active-order",
        ordered_at=NOW + timedelta(minutes=1),
    )
    xml = f"""<kaspi_catalog><offers>
      <offer sku='{owner.merchant_sku}'><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='2'/></offer>
      <offer sku='{wrong_member.merchant_sku}'><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='2'/></offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        merchant_id="shared-detach-account",
        source_filename="catalog.xml",
        source_xml=xml,
        generated_xml=xml,
        active=True,
    )
    batch = InventoryBatch(
        product_id=owner.id,
        received_at=NOW - timedelta(days=1),
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("2000"),
        is_received=True,
    )
    db_session.add_all(
        [
            feed,
            batch,
            DumpingPolicy(
                product_id=owner.id,
                enabled=True,
                auto_publish_xml=True,
            ),
            DumpingPolicy(
                product_id=wrong_member.id,
                enabled=True,
                auto_publish_xml=True,
            ),
        ]
    )
    db_session.flush()
    assert rebuild_product_fifo(db_session, product_id=owner.id) == 2

    detached = detach_product_inventory(wrong_member.id, db_session)

    db_session.refresh(owner)
    db_session.refresh(wrong_member)
    db_session.refresh(batch)
    assert owner.inventory_owner_product_id is None
    assert wrong_member.inventory_owner_product_id is None
    assert batch.product_id == owner.id
    assert batch.quantity_remaining == 1
    assert detached.inventory_owner_product_id == wrong_member.id
    assert detached.on_hand == 0
    assert [row.product_id for row in detached.shared_products] == [wrong_member.id]
    allocations = db_session.scalars(select(InventoryAllocation)).all()
    assert [row.marketplace_order_line_id for row in allocations] == [owner_line.id]
    assert wrong_line.id not in {
        row.marketplace_order_line_id for row in allocations
    }
    assert _offer_stock(feed.generated_xml, owner.merchant_sku or "") == ("yes", "1")
    assert _offer_stock(feed.generated_xml, wrong_member.merchant_sku or "") == ("no", "0")

    owner_inventory = get_product_inventory(owner.id, db_session)
    assert owner_inventory.on_hand == 1
    assert [row.product_id for row in owner_inventory.shared_products] == [owner.id]

    relinked = merge_product_inventory(
        wrong_member.id,
        InventoryOwnerUpdate(owner_product_id=correct_owner.id),
        db_session,
    )

    db_session.refresh(wrong_member)
    assert wrong_member.inventory_owner_product_id == correct_owner.id
    assert relinked.inventory_owner_product_id == correct_owner.id
    assert {row.product_id for row in relinked.shared_products} == {
        wrong_member.id,
        correct_owner.id,
    }


def test_detaching_inventory_owner_breaks_whole_group_and_keeps_its_batches(
    db_session,
) -> None:
    owner = _product(db_session, "OWNER-SPLIT")
    first_member = _product(db_session, "FIRST-MEMBER")
    second_member = _product(db_session, "SECOND-MEMBER")
    first_member.inventory_owner_product_id = owner.id
    second_member.inventory_owner_product_id = owner.id
    batch = InventoryBatch(
        product_id=owner.id,
        received_at=NOW,
        quantity_received=7,
        quantity_remaining=7,
        unit_cost=Decimal("1800"),
        is_received=True,
    )
    db_session.add(batch)
    db_session.flush()

    result = detach_product_inventory(owner.id, db_session)

    db_session.refresh(owner)
    db_session.refresh(first_member)
    db_session.refresh(second_member)
    db_session.refresh(batch)
    assert owner.inventory_owner_product_id is None
    assert first_member.inventory_owner_product_id is None
    assert second_member.inventory_owner_product_id is None
    assert batch.product_id == owner.id
    assert result.on_hand == 7
    assert [row.product_id for row in result.shared_products] == [owner.id]
    assert get_product_inventory(first_member.id, db_session).on_hand == 0
    assert get_product_inventory(second_member.id, db_session).on_hand == 0
