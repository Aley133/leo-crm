import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import pytest

from backend.app.dumping_models import DumpingPolicy, KaspiXmlFeed
from backend.app.inventory_models import InventoryBatch
from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.models import Product
from backend.app.product_xml_import_api import commit_xml_import


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


def test_kaspi_xml_parser_rejects_dtd_and_external_entities() -> None:
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_kaspi_products(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><offers/>')


def test_product_registry_exposes_preview_and_commit_import_endpoints() -> None:
    source = (ROOT / "backend" / "app" / "product_xml_import_api.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert 'prefix="/api/product-registry/imports/xml"' in source
    assert '@router.post("/preview")' in source
    assert '@router.post("/commit")' in source
    assert "db.commit()" not in source.split('@router.post("/preview")', 1)[1].split('@router.post("/commit")', 1)[0]
    assert "product_xml_import_router" in main
    assert "app.include_router(product_xml_import_router)" in main


def test_xml_commit_links_order_lines_in_one_bulk_pass() -> None:
    source = (ROOT / "backend" / "app" / "product_xml_import_api.py").read_text(encoding="utf-8")
    linking = (ROOT / "backend" / "app" / "order_line_product_linking.py").read_text(encoding="utf-8")

    assert "link_all_matching_order_lines_for_products" in source
    assert "for product in stored_products:" not in source
    assert "def link_all_matching_order_lines_for_products" in linking
    assert "identity_map = _product_identity_map(products)" in linking


def test_product_center_has_two_step_xml_import_ui() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for element_id in ('id="import-xml"', 'id="xml-file"', 'id="xml-dialog"', 'id="xml-preview"', 'id="confirm-import"'):
        assert element_id in html
    assert "/api/product-registry/imports/xml/${action}" in script
    assert 'xmlRequest("preview", file)' in script
    assert 'xmlRequest("commit", selectedXmlFile)' in script


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

    result = asyncio.run(
        commit_xml_import(_XmlRequest(uploaded, filename="corrected.xml"), db_session)
    )
    db_session.refresh(feed)

    assert result["total"] == 3
    assert feed.source_filename == "corrected.xml"
    assert feed.source_xml == uploaded.decode("utf-8")
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
