from pathlib import Path

import pytest

from backend.app.kaspi_xml_import import parse_kaspi_products


ROOT = Path(__file__).resolve().parents[1]


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


def test_product_center_has_two_step_xml_import_ui() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for element_id in ('id="import-xml"', 'id="xml-file"', 'id="xml-dialog"', 'id="xml-preview"', 'id="confirm-import"'):
        assert element_id in html
    assert "/api/product-registry/imports/xml/${action}" in script
    assert 'xmlRequest("preview", file)' in script
    assert 'xmlRequest("commit", selectedXmlFile)' in script
