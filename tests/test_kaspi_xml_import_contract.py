import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from backend.app.dumping_models import DumpingPolicy, KaspiXmlFeed
from backend.app.dumping_service import set_product_sale_enabled
from backend.app.inventory_models import InventoryBatch
from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.models import Product
from backend.app.product_xml_import_api import _commit_xml_import
from backend.app import product_xml_import_api


ROOT = Path(__file__).resolve().parents[1]


class _XmlRequest:
    def __init__(self, body: bytes, *, filename: str = "catalog.xml") -> None:
        self._body = body
        self.headers = {"X-Filename": filename}

    async def body(self) -> bytes:
        return self._body


def _offer_state(xml_text: str, sku: str) -> dict[str, str | None]:
    root = ElementTree.fromstring(xml_text)
    offer = next(
        node
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() == "offer"
        and node.attrib.get("sku") == sku
    )
    price = next(
        node.text
        for node in offer.iter()
        if node.tag.rsplit("}", 1)[-1].lower() == "cityprice"
    )
    availability = next(
        node
        for node in offer.iter()
        if node.tag.rsplit("}", 1)[-1].lower() == "availability"
    )
    return {
        "price": price,
        "available": availability.attrib.get("available"),
        "preOrder": availability.attrib.get("preOrder"),
        "stockCount": availability.attrib.get("stockCount"),
    }


def test_kaspi_xml_parser_reads_offer_identity_and_product_fields() -> None:
    products, warnings = parse_kaspi_products(
        b'''<?xml version="1.0" encoding="UTF-8"?>
        <kaspi_catalog><offers>
          <offer sku="131846482"><model>SOLAB Magnesium</model><brand>SOLAB</brand><price>5990</price></offer>
        </offers></kaspi_catalog>'''
    )

    assert warnings == []
    assert len(products) == 1
    assert products[0].kaspi_product_id == "131846482"
    assert products[0].merchant_sku == "131846482"
    assert products[0].name == "SOLAB Magnesium"
    assert products[0].brand == "SOLAB"
    assert products[0].available is None


def test_kaspi_xml_parser_rejects_dtd_and_external_entities() -> None:
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_kaspi_products(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><offers/>')


def test_product_registry_exposes_preview_and_commit_import_endpoints() -> None:
    source = (ROOT / "backend" / "app" / "product_xml_import_api.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert 'prefix="/api/product-registry/imports/xml"' in source
    assert '@router.post("/preview")' in source
    assert '@router.post("/commit")' in source
    preview_route = source.split('@router.post("/preview")', 1)[1].split(
        "def _commit_xml_import",
        1,
    )[0]
    assert "db.commit()" not in preview_route
    assert "product_xml_import_router" in main
    assert "app.include_router(product_xml_import_router)" in main


def test_xml_commit_does_not_block_the_async_web_loop(monkeypatch) -> None:
    def slow_worker(_operation):
        time.sleep(0.08)
        return {"committed": True}

    monkeypatch.setattr(product_xml_import_api, "_with_session", slow_worker)

    async def scenario() -> None:
        request = _XmlRequest(b"<kaspi_catalog><offers/></kaspi_catalog>")
        task = asyncio.create_task(product_xml_import_api.commit_xml_import(request))
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert await task == {"committed": True}

    asyncio.run(scenario())


def test_xml_commit_links_order_lines_in_one_bulk_pass() -> None:
    source = (ROOT / "backend" / "app" / "product_xml_import_api.py").read_text(encoding="utf-8")
    linking = (ROOT / "backend" / "app" / "order_line_product_linking.py").read_text(encoding="utf-8")

    assert "link_all_matching_order_lines_for_products" in source
    assert "for product in stored_products:" not in source
    assert "def link_all_matching_order_lines_for_products" in linking
    assert "identity_map = _product_identity_map(products)" in linking
    assert "MarketplaceOrderLine.merchant_sku.in_(batch)" in linking
    assert "MarketplaceOrderLine.external_product_id.in_(batch)" in linking
    assert "MarketplaceOrderLine.merchant_sku.is_not(None)" not in linking


def test_product_center_has_two_step_xml_import_ui() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for element_id in ('id="import-xml"', 'id="xml-file"', 'id="xml-dialog"', 'id="xml-preview"', 'id="confirm-import"'):
        assert element_id in html
    assert "/api/product-registry/imports/xml/${action}" in script
    assert 'xmlRequest("preview", file)' in script
    assert 'xmlRequest("commit", selectedXmlFile)' in script
    assert 'body:file' in script
    assert "retainXmlSource" not in script
    assert 'xmlRequest("retain-source"' not in script


def test_xml_reimport_uses_new_source_and_overlays_only_managed_offers(
    db_session,
) -> None:
    managed = Product(
        kaspi_product_id="MANAGED",
        merchant_sku="MANAGED",
        name="Managed product",
        status="active",
    )
    unmanaged = Product(
        kaspi_product_id="UNMANAGED",
        merchant_sku="UNMANAGED",
        name="Unmanaged product",
        status="active",
    )
    disabled = Product(
        kaspi_product_id="DISABLED",
        merchant_sku="DISABLED",
        name="Disabled dumping product",
        status="active",
    )
    db_session.add_all([managed, unmanaged, disabled])
    db_session.flush()
    db_session.add_all(
        [
            DumpingPolicy(
                product_id=managed.id,
                enabled=True,
                auto_publish_xml=True,
            ),
            DumpingPolicy(
                product_id=disabled.id,
                enabled=False,
                auto_publish_xml=True,
            ),
            InventoryBatch(
                product_id=managed.id,
                received_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
                quantity_received=3,
                quantity_remaining=3,
                unit_cost=Decimal("1000"),
            ),
            InventoryBatch(
                product_id=unmanaged.id,
                received_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
                quantity_received=8,
                quantity_remaining=8,
                unit_cost=Decimal("1000"),
            ),
            InventoryBatch(
                product_id=disabled.id,
                received_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
                quantity_received=6,
                quantity_remaining=6,
                unit_cost=Decimal("1000"),
            ),
        ]
    )
    old_source = """<kaspi_catalog><offers>
      <offer sku='MANAGED'><cityprices><cityprice cityId='750000000'>1</cityprice></cityprices><availability available='no' preOrder='0' stockCount='0'/></offer>
      <offer sku='UNMANAGED'><cityprices><cityprice cityId='750000000'>1</cityprice></cityprices><availability available='no' preOrder='0' stockCount='0'/></offer>
      <offer sku='DISABLED'><cityprices><cityprice cityId='750000000'>1</cityprice></cityprices><availability available='no' preOrder='0' stockCount='0'/></offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="old.xml",
        source_xml=old_source,
        generated_xml=old_source,
        active=True,
    )
    db_session.add(feed)
    db_session.commit()

    uploaded = b"""<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><merchantid>merchant-1</merchantid><offers>
      <offer sku='MANAGED'><model>Managed product</model><cityprices><cityprice cityId='750000000'>9000</cityprice></cityprices><availability available='no' preOrder='7' stockCount='91'/></offer>
      <offer sku='UNMANAGED'><model>Unmanaged product</model><cityprices><cityprice cityId='750000000'>7200</cityprice></cityprices><availability available='yes' preOrder='6' stockCount='41'/></offer>
      <offer sku='DISABLED'><model>Disabled dumping product</model><cityprices><cityprice cityId='750000000'>6300</cityprice></cityprices><availability available='yes' preOrder='5' stockCount='31'/></offer>
    </offers></kaspi_catalog>"""

    result = _commit_xml_import(
        uploaded,
        source_filename="corrected.xml",
        db=db_session,
    )
    db_session.refresh(feed)

    assert result["total"] == 3
    assert feed.source_filename == "corrected.xml"
    assert _offer_state(feed.source_xml, "MANAGED") == {
        "price": "9000",
        "available": "no",
        "preOrder": "7",
        "stockCount": "91",
    }
    assert _offer_state(feed.generated_xml, "MANAGED") == {
        "price": "9000",
        "available": "yes",
        "preOrder": "0",
        "stockCount": "3",
    }
    assert _offer_state(feed.generated_xml, "UNMANAGED") == {
        "price": "7200",
        "available": "yes",
        "preOrder": "6",
        "stockCount": "41",
    }
    assert _offer_state(feed.generated_xml, "DISABLED") == {
        "price": "6300",
        "available": "yes",
        "preOrder": "5",
        "stockCount": "31",
    }


def test_xml_import_accumulates_catalog_and_keeps_new_unavailable_items_out_of_dumping(
    db_session,
) -> None:
    first = b"""<kaspi_catalog><merchantid>merchant-1</merchantid><offers>
      <offer sku='A'><model>Available A</model><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='4'/></offer>
    </offers></kaspi_catalog>"""
    second = b"""<kaspi_catalog><merchantid>merchant-1</merchantid><offers>
      <offer sku='B'><model>Unavailable B</model><cityprices><cityprice cityId='750000000'>6000</cityprice></cityprices><availability available='no' preOrder='0' stockCount='0'/></offer>
    </offers></kaspi_catalog>"""

    _commit_xml_import(first, source_filename="available.xml", db=db_session)
    result = _commit_xml_import(second, source_filename="unavailable.xml", db=db_session)

    products = {
        product.kaspi_product_id: product
        for product in db_session.query(Product).order_by(Product.id).all()
    }
    feed = db_session.query(KaspiXmlFeed).one()
    assert set(products) == {"A", "B"}
    assert products["A"].sale_enabled is True
    assert products["B"].sale_enabled is False
    assert result["created_count"] == 1
    assert result["retained_count"] == 1
    assert result["catalog_total"] == 2
    assert _offer_state(feed.source_xml, "A")["available"] == "yes"
    assert _offer_state(feed.source_xml, "B")["available"] == "no"
    assert _offer_state(feed.generated_xml, "A")["available"] == "yes"
    assert _offer_state(feed.generated_xml, "B")["available"] == "no"
    assert db_session.query(DumpingPolicy).count() == 0

    set_product_sale_enabled(
        db_session,
        product_id=products["B"].id,
        sale_enabled=True,
    )
    db_session.commit()
    third = b"""<kaspi_catalog><merchantid>merchant-1</merchantid><offers>
      <offer sku='C'><model>Product C</model><cityprices><cityprice cityId='750000000'>7000</cityprice></cityprices><availability available='no' preOrder='0' stockCount='0'/></offer>
    </offers></kaspi_catalog>"""
    _commit_xml_import(third, source_filename="third.xml", db=db_session)
    db_session.refresh(feed)
    db_session.refresh(products["B"])
    assert products["B"].sale_enabled is True
    assert products["B"].sale_state_overridden is True
    assert _offer_state(feed.generated_xml, "B")["available"] == "yes"
    assert _offer_state(feed.generated_xml, "B")["stockCount"] == "1"


def test_manual_out_of_stock_survives_xml_reimport_and_resumes_without_losing_policy(
    db_session,
) -> None:
    initial = b"""<kaspi_catalog><merchantid>merchant-1</merchantid><offers>
      <offer sku='A'><model>Product A</model><cityprices><cityprice cityId='750000000'>5000</cityprice></cityprices><availability available='yes' preOrder='0' stockCount='2'/></offer>
    </offers></kaspi_catalog>"""
    _commit_xml_import(initial, source_filename="initial.xml", db=db_session)
    product = db_session.query(Product).filter_by(kaspi_product_id="A").one()
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
    )
    batch = InventoryBatch(
        product_id=product.id,
        received_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("1000"),
    )
    db_session.add_all([policy, batch])
    db_session.commit()

    stopped = set_product_sale_enabled(
        db_session,
        product_id=product.id,
        sale_enabled=False,
    )
    db_session.commit()
    feed = db_session.query(KaspiXmlFeed).one()
    assert stopped["xml_state"] == "manual_out_of_stock"
    assert _offer_state(feed.generated_xml, "A")["available"] == "no"
    assert _offer_state(feed.generated_xml, "A")["stockCount"] == "0"

    updated = initial.replace(b">5000<", b">5500<")
    _commit_xml_import(updated, source_filename="updated.xml", db=db_session)
    db_session.refresh(product)
    db_session.refresh(policy)
    db_session.refresh(feed)
    assert product.sale_enabled is False
    assert policy.enabled is True
    assert _offer_state(feed.generated_xml, "A")["available"] == "no"

    resumed = set_product_sale_enabled(
        db_session,
        product_id=product.id,
        sale_enabled=True,
    )
    db_session.commit()
    db_session.refresh(feed)
    assert resumed["xml_state"] == "stock"
    assert _offer_state(feed.generated_xml, "A")["available"] == "yes"
    assert _offer_state(feed.generated_xml, "A")["stockCount"] == "2"
    assert policy.enabled is True
