import asyncio
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from sqlalchemy import select

from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.fast_dumping_agent_api import FastAgentIdentity
from backend.app.models import Product
from backend.app.product_images import normalize_product_image_url
from backend.app.product_registry_api import resolve_product_image
from backend.app.product_test_api import (
    ProductTestInspectRequest,
    build_product_test_xml,
    claim_product_test_job,
    inspect_product,
)
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app import product_test_api
from tools import kaspi_fast_dumping_scanner
from tools.kaspi_fast_dumping_scanner import (
    _meta_content,
    _open_product_page,
    _product_id_from_url,
)


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


def test_public_photo_card_does_not_require_promo_conditions(monkeypatch) -> None:
    calls: list[str] = []
    card_html = (
        '<html><head><meta property="og:image" '
        'content="https://resources.cdn-kaspi.kz/img/p/photo.jpg"></head></html>'
    )

    async def fake_request(_client, _method, url, **_kwargs):
        calls.append(url)
        canonical = "https://kaspi.kz/shop/p/test-product-110563850/?c=196220100"
        return httpx.Response(
            200,
            text=card_html,
            request=httpx.Request("GET", canonical),
        )

    monkeypatch.setattr(kaspi_fast_dumping_scanner, "_request_with_retry", fake_request)
    page, promo, product_url = asyncio.run(
        _open_product_page(
            object(),
            master_id="110563850",
            city_id="196220100",
            product_name_hint=None,
            require_promo=False,
        )
    )

    assert len(calls) == 1
    assert promo == {}
    assert _product_id_from_url(product_url) == "110563850"
    assert _meta_content(page.text, "property", "og:image") == "https://resources.cdn-kaspi.kz/img/p/photo.jpg"


def test_fast_dumping_still_requires_promo_conditions(monkeypatch) -> None:
    async def fake_request(_client, _method, url, **_kwargs):
        return httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(kaspi_fast_dumping_scanner, "_request_with_retry", fake_request)
    with pytest.raises(ValueError, match="not resolved"):
        asyncio.run(
                _open_product_page(
                    object(),
                master_id="110563850",
                city_id="196220100",
                product_name_hint=None,
            )
        )


def test_legacy_product_test_agent_claim_is_retired() -> None:
    result = claim_product_test_job(
        FastAgentIdentity(agent_id="agent-w2", workspace_id=2, merchant_uid="merchant-2"),
    )
    assert result == {"job": None, "retired": True}


def test_product_test_ui_uses_direct_http_without_fast_agent() -> None:
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ui = (ROOT / "backend/app/ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend/app/static/product-test.html").read_text(encoding="utf-8")
    script = (ROOT / "backend/app/static/product-test.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    assert "app.include_router(product_test_router)" in main
    assert '@router.get("/crm/product-test"' in ui
    assert 'id="inspect-form"' in html
    assert 'id="download-xml"' in html
    assert "ПРЯМОЙ HTTP" in html
    assert "/api/product-test/inspect" in script
    assert "/api/fast-dumping-agent/agents/status" not in script
    assert "/api/product-test-agent/claim" not in agent
    assert "inspect_kaspi_product" not in agent
    api = (ROOT / "backend/app/product_test_api.py").read_text(encoding="utf-8")
    assert "await inspect_kaspi_product" in api
    assert "ProductTestJob.workspace_id == payload.workspace_id" in api
    assert "ProductTestItem.workspace_id == job.workspace_id" in api
    assert "Product.workspace_id == job.workspace_id" in api


def test_product_test_inspection_runs_directly_in_crm(db_session, monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_inspect(**options):
        calls.append(options)
        return {
            "kaspi_product_id": "102591400",
            "merchant_sku": "102591400_177620711",
            "product_name": "Test product",
            "brand": "LEO",
            "image_url": "https://resources.cdn-kaspi.kz/img/direct-http.jpg",
            "product_url": "https://kaspi.kz/shop/p/test-product-102591400/",
            "page_visible_price_kzt": "8538",
            "offers": {"seller_count_scanned": 0, "top_offers": []},
        }

    monkeypatch.setattr(product_test_api, "inspect_kaspi_product", fake_inspect)
    result = asyncio.run(
        inspect_product(
            ProductTestInspectRequest(reference="102591400_177620711"),
            db_session,
        )
    )

    assert calls == [{
        "reference": "102591400_177620711",
        "city_id": "196220100",
        "zone_id": "Magnum_ZONE1",
    }]
    assert result["job"]["status"] == "succeeded"
    assert result["item"]["image_url"] == "https://resources.cdn-kaspi.kz/img/direct-http.jpg"
    job = db_session.scalar(select(ProductTestJob))
    assert job is not None
    assert job.agent_id == "crm-http"


def test_missing_product_photo_is_resolved_once_and_cached(db_session, monkeypatch) -> None:
    product = Product(
        workspace_id=1,
        kaspi_product_id="110563850",
        merchant_sku="110563850_272949101",
        name="Test existing product",
    )
    db_session.add(product)
    db_session.commit()
    calls: list[dict] = []

    async def fake_inspect(**options):
        calls.append(options)
        return {"image_url": "https://resources.cdn-kaspi.kz/img/existing-product.jpg"}

    monkeypatch.setattr("backend.app.product_registry_api.inspect_kaspi_product", fake_inspect)
    first = asyncio.run(resolve_product_image(product.id, db_session))
    second = asyncio.run(resolve_product_image(product.id, db_session))

    assert first.image_url == "https://resources.cdn-kaspi.kz/img/existing-product.jpg"
    assert first.cached is False
    assert second.cached is True
    assert calls == [{
        "reference": "110563850",
        "city_id": "196220100",
        "zone_id": "Magnum_ZONE1",
        "max_pages": 0,
    }]
    db_session.refresh(product)
    assert product.image_url == first.image_url


def test_product_photos_are_lazy_in_core_crm_surfaces() -> None:
    for filename in ("products.js", "orders.js", "dumping.js", "fast-dumping.js", "product-test.js"):
        source = (ROOT / "backend/app/static" / filename).read_text(encoding="utf-8")
        assert 'loading="lazy"' in source
        assert 'referrerpolicy="no-referrer"' in source
    detail = (ROOT / "backend/app/static/product-detail.html").read_text(encoding="utf-8")
    assert 'id="product-photo"' in detail
    assert 'loading="lazy"' in detail
    resolver = (ROOT / "backend/app/static/product-image-resolver.js").read_text(encoding="utf-8")
    assert "IntersectionObserver" in resolver
    assert "activeRequests < 2" in resolver
    assert "/resolve-image" in resolver
    for filename in ("orders.html", "products.html", "product-detail.html", "dumping.html", "fast-dumping.html"):
        page = (ROOT / "backend/app/static" / filename).read_text(encoding="utf-8")
        assert "product-image-resolver.js" in page
    photo_css = (ROOT / "backend/app/static/product-images.css").read_text(encoding="utf-8")
    assert ".product-thumb,.order-product-photo,.product-photo,.fast-product-photo,.dumping-product-photo{width:100px;height:100px" in photo_css
