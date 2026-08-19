from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.fast_dumping_agent_api import FastAgentIdentity
from backend.app.product_images import normalize_product_image_url
from backend.app.product_test_api import build_product_test_xml, claim_product_test_job
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app import product_test_api
from tools.kaspi_fast_dumping_scanner import _meta_content, _product_id_from_url


ROOT = Path(__file__).resolve().parents[1]


def _children(element):
    return [str(child.tag).rsplit("}", 1)[-1] for child in list(element)]


def test_product_image_urls_are_https_and_kaspi_owned() -> None:
    valid = "https://resources.cdn-kaspi.kz/img/m/p/test.jpg"
    assert normalize_product_image_url(valid) == valid
    assert normalize_product_image_url("http://resources.cdn-kaspi.kz/test.jpg") is None
    assert normalize_product_image_url("https://evil.example/test.jpg") is None
    assert normalize_product_image_url("data:image/png;base64,AAAA") is None


def test_xml_import_reads_picture_without_storing_bytes() -> None:
    products, warnings = parse_kaspi_products(
        b"""<kaspi_catalog><offers><offer sku='123456'>
        <model>Test product</model><picture>https://resources.cdn-kaspi.kz/img/test.jpg</picture>
        </offer></offers></kaspi_catalog>"""
    )
    assert warnings == []
    assert products[0].image_url == "https://resources.cdn-kaspi.kz/img/test.jpg"


def test_product_test_xml_is_a_copy_with_schema_safe_offer() -> None:
    source = """<kaspi_catalog><merchantid>merchant</merchantid><offers>
      <offer sku='BASE'><model>Base</model><availabilities><availability available='yes' storeId='PP1' preOrder='0' stockCount='1'/></availabilities><cityprices><cityprice cityId='196220100'>1000</cityprice></cityprices></offer>
    </offers></kaspi_catalog>"""
    item = ProductTestItem(
        input_reference="102591400_177620711",
        kaspi_product_id="102591400",
        merchant_sku="102591400_177620711",
        name="New product",
        brand="LEO",
        image_url="https://resources.cdn-kaspi.kz/img/new.jpg",
        kaspi_url="https://kaspi.kz/shop/p/product-102591400/",
        observed_price_kzt=Decimal("8538"),
        test_price_kzt=Decimal("8990"),
        preorder_days=3,
        stock_count=7,
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={},
        active=True,
    )

    rendered = build_product_test_xml(source, [item])
    root = ElementTree.fromstring(rendered)
    offers = [node for node in root.iter() if str(node.tag).rsplit("}", 1)[-1] == "offer"]
    assert {node.attrib["sku"] for node in offers} == {"BASE", "102591400_177620711"}
    added = next(node for node in offers if node.attrib["sku"] == "102591400_177620711")
    assert _children(added).index("availabilities") < _children(added).index("cityprices")
    availability = next(node for node in added.iter() if str(node.tag).rsplit("}", 1)[-1] == "availability")
    assert availability.attrib == {"available": "yes", "storeId": "PP1", "preOrder": "3", "stockCount": "7"}
    cityprice = next(node for node in added.iter() if str(node.tag).rsplit("}", 1)[-1] == "cityprice")
    assert cityprice.attrib["cityId"] == "196220100"
    assert cityprice.text == "8990"
    assert "102591400_177620711" not in source


def test_agent_reader_supports_composite_and_full_kaspi_references() -> None:
    url = "https://kaspi.kz/shop/m/Zecar/products?productCode=101268790&masterSku=101268790&merchantSku=101268790_498068984"
    assert _product_id_from_url(url) == "101268790"
    html = '<meta property="og:image" content="https://resources.cdn-kaspi.kz/img/p.jpg">'
    assert _meta_content(html, "property", "og:image") == "https://resources.cdn-kaspi.kz/img/p.jpg"


def test_product_test_agent_claim_is_workspace_isolated(db_session, monkeypatch) -> None:
    db_session.info["include_all_workspaces"] = True
    first = ProductTestJob(workspace_id=1, input_reference="111111", city_id="196220100", zone_id="Magnum_ZONE1", status="queued", result_json={})
    second = ProductTestJob(workspace_id=2, input_reference="222222", city_id="196220100", zone_id="Magnum_ZONE1", status="queued", result_json={})
    db_session.add_all([first, second])
    db_session.commit()
    monkeypatch.setattr(product_test_api, "_validate_workspace_merchant", lambda *_args, **_kwargs: None)

    result = claim_product_test_job(
        FastAgentIdentity(agent_id="agent-w2", workspace_id=2, merchant_uid="merchant-2"),
        db_session,
    )

    assert result["job"]["id"] == second.id
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.status == "queued"
    assert second.status == "leased"


def test_product_test_ui_and_agent_contract_are_wired() -> None:
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ui = (ROOT / "backend/app/ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend/app/static/product-test.html").read_text(encoding="utf-8")
    script = (ROOT / "backend/app/static/product-test.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    assert "app.include_router(product_test_router)" in main
    assert "app.include_router(product_test_agent_router)" in main
    assert '@router.get("/crm/product-test"' in ui
    assert 'id="inspect-form"' in html
    assert 'id="download-xml"' in html
    assert "/api/product-test/inspect" in script
    assert "/api/product-test-agent/claim" in agent
    assert "inspect_kaspi_product" in agent
    api = (ROOT / "backend/app/product_test_api.py").read_text(encoding="utf-8")
    assert "ProductTestJob.workspace_id == payload.workspace_id" in api
    assert "ProductTestItem.workspace_id == payload.workspace_id" in api
    assert "Product.workspace_id == payload.workspace_id" in api


def test_product_photos_are_lazy_in_core_crm_surfaces() -> None:
    for filename in ("products.js", "orders.js", "dumping.js", "fast-dumping.js", "product-test.js"):
        source = (ROOT / "backend/app/static" / filename).read_text(encoding="utf-8")
        assert 'loading="lazy"' in source
        assert 'referrerpolicy="no-referrer"' in source
    detail = (ROOT / "backend/app/static/product-detail.html").read_text(encoding="utf-8")
    assert 'id="product-photo"' in detail
    assert 'loading="lazy"' in detail
