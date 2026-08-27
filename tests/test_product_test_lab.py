import asyncio
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from sqlalchemy import select

from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.fast_dumping_agent_api import FastAgentIdentity
from backend.app.models import MarketplaceAccount, Product
from backend.app.product_images import normalize_product_image_url
from backend.app.product_registry_api import resolve_product_image
from backend.app import kaspi_product_photo
from backend.app.product_test_api import (
    ProductDiscoveryRequest,
    ProductTestAgentHeartbeat,
    ProductTestInspectRequest,
    ProductTestUpdate,
    ProductTestAgentIdentity,
    ProductTestAgentResult,
    add_product_to_kaspi,
    build_product_test_xml,
    claim_product_test_job,
    complete_product_test_job,
    discover_product_candidates,
    heartbeat_product_test_agent,
    inspect_product,
    update_product_test_item,
    validate_product_supplier,
)
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app.fast_dumping_models import FastDumpingJob, FastDumpingPolicy
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.product_test_pricing import choose_initial_offer_price
from backend.app import product_test_api
from backend.app.workspace_models import KaspiAccountCredential, Workspace
from backend.app.workspace_context import workspace_context
from tools import kaspi_fast_dumping_scanner
from tools.product_discovery import kaspi_search, runtime as product_discovery_runtime
from tools.kaspi_fast_dumping_scanner import (
    _meta_content,
    _open_product_page,
    _product_id_from_url,
)


ROOT = Path(__file__).resolve().parents[1]


def _children(element):
    return [str(child.tag).rsplit("}", 1)[-1] for child in list(element)]


def _seed_agent_account(db_session, *, workspace_id: int = 1, partner_id: str = "merchant-1") -> None:
    db_session.info["include_all_workspaces"] = True
    workspace = Workspace(
        id=workspace_id,
        name=f"Workspace {workspace_id}",
        slug=f"workspace-{workspace_id}",
        is_active=True,
    )
    account = MarketplaceAccount(
        workspace_id=workspace_id,
        provider="kaspi",
        external_account_id=partner_id,
        display_name=partner_id,
        timezone="Asia/Almaty",
    )
    db_session.add_all((workspace, account))
    db_session.flush()
    db_session.add(
        KaspiAccountCredential(
            workspace_id=workspace_id,
            marketplace_account_id=account.id,
            partner_id=partner_id,
            api_token_encrypted="test",
        )
    )
    db_session.commit()


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


def test_kaspi_discovery_retries_one_rate_limited_page(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []
    search = kaspi_search.KaspiProductSearch("196220100", page_delay_ms=0)

    def fake_get(url, **_kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("GET", url)
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    monkeypatch.setattr(search.client, "get", fake_get)
    monkeypatch.setattr(kaspi_search.time, "sleep", sleeps.append)
    try:
        response, _url, _params, retries = search._page_with_rate_limit_retry(
            "Solgar",
            1,
            "request-id",
        )
    finally:
        search.close()

    assert response.status_code == 200
    assert retries == 1
    assert calls == 2
    assert sleeps == [0.8]


def test_backend_photo_reader_prefers_mobile_json_endpoint(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "card": {"id": "102020267"},
                    "galleryImages": [{
                        "large": "https://resources.cdn-kaspi.kz/img/m/p/photo.jpg",
                    }],
                },
            },
            request=request,
        )

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(kaspi_product_photo.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(kaspi_product_photo, "_REQUEST_SPACING_SECONDS", 0)
    kaspi_product_photo._NEXT_REQUEST_AT = 0
    image_url = asyncio.run(
        kaspi_product_photo.fetch_kaspi_product_photo(
            kaspi_product_id="102020267",
            product_name="GLS Pharmaceuticals Кальций D3 600 мг",
        )
    )

    assert image_url == "https://resources.cdn-kaspi.kz/img/m/p/photo.jpg"
    assert len(requests) == 1
    assert requests[0].url.path == "/shop/rest/misc/product/mobile"
    assert requests[0].url.params["productCode"] == "102020267"
    assert requests[0].url.params["cityId"] == "196220100"


def test_backend_photo_reader_falls_back_to_public_card(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/shop/rest/misc/product/mobile":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            text=(
                '<html><head><meta property="og:image" '
                'content="https://resources.cdn-kaspi.kz/img/m/p/fallback.jpg"></head></html>'
            ),
            request=httpx.Request(
                "GET",
                "https://kaspi.kz/shop/p/test-product-102020267/?c=196220100",
            ),
        )

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(kaspi_product_photo.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(kaspi_product_photo, "_REQUEST_SPACING_SECONDS", 0)
    kaspi_product_photo._NEXT_REQUEST_AT = 0
    image_url = asyncio.run(
        kaspi_product_photo.fetch_kaspi_product_photo(
            kaspi_product_id="102020267",
            product_name="Test product",
        )
    )

    assert image_url == "https://resources.cdn-kaspi.kz/img/m/p/fallback.jpg"
    assert len(requests) == 2
    assert requests[1].url.path.endswith("-102020267/")


def test_backend_photo_reader_does_not_amplify_mobile_rate_limit(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, request=request)

    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(kaspi_product_photo.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(kaspi_product_photo, "_REQUEST_SPACING_SECONDS", 0)
    kaspi_product_photo._NEXT_REQUEST_AT = 0

    with pytest.raises(kaspi_product_photo.KaspiPhotoReadError, match="429"):
        asyncio.run(
            kaspi_product_photo.fetch_kaspi_product_photo(
                kaspi_product_id="102020267",
                product_name="Test product",
            )
        )

    assert len(requests) == 1
    assert requests[0].url.path == "/shop/rest/misc/product/mobile"


def test_backend_photo_reader_preserves_empty_timeout_name() -> None:
    assert kaspi_product_photo._error_text(asyncio.TimeoutError()) == "TimeoutError"


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


def test_product_test_agent_claim_leases_workspace_job(db_session) -> None:
    _seed_agent_account(db_session)
    queued = inspect_product(
        ProductTestInspectRequest(reference="102591400_177620711"),
        db_session,
    )
    legacy_claim = claim_product_test_job(
        FastAgentIdentity(agent_id="fast-w1", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert legacy_claim["job"] is None
    assert legacy_claim["agent_required"] == "product_test"
    result = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert queued["job"]["status"] == "queued"
    assert result["job"]["reference"] == "102591400_177620711"
    assert len(result["job"]["lease_token"]) == 32


def test_product_test_agent_has_independent_presence(db_session) -> None:
    _seed_agent_account(db_session)
    product_test_api._PRODUCT_TEST_HEARTBEATS.clear()
    record = heartbeat_product_test_agent(
        ProductTestAgentHeartbeat(
            agent_id="product-test-pc",
            agent_kind="product_test",
            workspace_id=1,
            merchant_uid="merchant-1",
            hostname="LAB-PC",
            version="1.0.0",
        ),
        db_session,
    )
    status = product_test_api._product_test_agent_status(1)
    assert record["agent_kind"] == "product_test"
    assert status["online"] is True
    assert status["agents"][0]["version"] == "1.0.0"


def test_two_fast_agents_cannot_cross_workspace_product_test_jobs(db_session) -> None:
    _seed_agent_account(db_session, workspace_id=1, partner_id="merchant-1")
    _seed_agent_account(db_session, workspace_id=3, partner_id="merchant-3")

    with workspace_context(1):
        first = inspect_product(
            ProductTestInspectRequest(reference="111111111_111111111"),
            db_session,
        )
    with workspace_context(3):
        third = inspect_product(
            ProductTestInspectRequest(reference="333333333_333333333"),
            db_session,
        )

    third_claim = claim_product_test_job(
        ProductTestAgentIdentity(
            agent_id="agent-w3",
            agent_kind="product_test",
            workspace_id=3,
            merchant_uid="merchant-3",
        ),
        db_session,
    )
    first_claim = claim_product_test_job(
        ProductTestAgentIdentity(
            agent_id="agent-w1",
            agent_kind="product_test",
            workspace_id=1,
            merchant_uid="merchant-1",
        ),
        db_session,
    )

    assert third["job"]["id"] == third_claim["job"]["id"]
    assert third_claim["job"]["reference"] == "333333333_333333333"
    assert first["job"]["id"] == first_claim["job"]["id"]
    assert first_claim["job"]["reference"] == "111111111_111111111"

    with pytest.raises(product_test_api.HTTPException) as caught:
        complete_product_test_job(
            first_claim["job"]["id"],
            ProductTestAgentResult(
                agent_id="agent-w3",
                workspace_id=3,
                lease_token=first_claim["job"]["lease_token"],
                status="failed",
                error_message="must stay isolated",
            ),
            db_session,
        )
    assert caught.value.status_code == 404


def test_product_test_ui_uses_local_fast_agent() -> None:
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ui = (ROOT / "backend/app/ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend/app/static/product-test.html").read_text(encoding="utf-8")
    script = (ROOT / "backend/app/static/product-test.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/product_test_agent.py").read_text(encoding="utf-8")
    fast_agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    assert "app.include_router(product_test_router)" in main
    assert '@router.get("/crm/product-test"' in ui
    assert 'id="discover-form"' in html
    assert 'id="settings-form"' in html
    assert "ОТДЕЛЬНЫЙ PRODUCT TEST AGENT" in html
    assert "LEO-Product-Test-Agent.exe" in html
    assert "/api/product-test/discover" in script
    assert "/validate-supplier" in script
    assert "/add`" in script
    assert 'name="max_undercut_gap_percent"' in html
    assert 'name="delivery_price_premium_kzt"' in html
    assert 'name="delivery_advantage_days"' in html
    assert "renderAgent(payload.agent" in script
    assert "Проверить / заменить ссылку" in script
    assert "Выгрузить на Kaspi" in script
    assert "lab-results-table" in script
    assert "supplier_rating" in script
    assert "supplier_delivery_text" in script
    assert 'job.job_type !== "inspect"' in script
    assert "supplier_image_url" in script
    assert 'href="/crm/monitoring"' in script
    assert "ВИЗУАЛЬНОЕ СОПОСТАВЛЕНИЕ" in html
    assert "/api/product-test-agent/claim" in agent
    assert "inspect_kaspi_product" in agent
    assert "discover_products" in agent
    assert "create_linked_offer" in agent
    assert "/api/product-test-agent/claim" not in fast_agent
    api = (ROOT / "backend/app/product_test_api.py").read_text(encoding="utf-8")
    assert 'status="queued"' in api
    assert "ProductTestJob.workspace_id == payload.workspace_id" in api
    assert "ProductTestItem.workspace_id == job.workspace_id" in api
    assert "Product.workspace_id == job.workspace_id" in api


def test_product_test_inspection_completes_from_local_agent(db_session) -> None:
    _seed_agent_account(db_session)
    queued = inspect_product(
        ProductTestInspectRequest(reference="102591400_177620711"),
        db_session,
    )
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    result = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="succeeded",
            result={
                "kaspi_product_id": "102591400",
                "merchant_sku": "102591400_177620711",
                "product_name": "Test product",
                "brand": "LEO",
                "image_url": "https://resources.cdn-kaspi.kz/img/local-agent.jpg",
                "product_url": "https://kaspi.kz/shop/p/test-product-102591400/",
                "page_visible_price_kzt": "8538",
                "offers": {"seller_count_scanned": 0, "top_offers": []},
            },
        ),
        db_session,
    )

    assert queued["queued"] is True
    assert result["job"]["status"] == "succeeded"
    assert result["item"]["image_url"] == "https://resources.cdn-kaspi.kz/img/local-agent.jpg"
    job = db_session.scalar(select(ProductTestJob))
    assert job is not None
    assert job.agent_id == "agent-w1"
    assert job.result_json == {
        "kaspi_product_id": "102591400",
        "merchant_sku": "102591400_177620711",
    }


def test_initial_offer_price_has_only_two_market_outcomes() -> None:
    can_undercut = choose_initial_offer_price(
        supplier_cost_kzt=Decimal("3000"),
        minimum_profit_kzt=Decimal("1000"),
        competitor_price_kzt=Decimal("9000"),
        undercut_step_kzt=1,
    )
    assert can_undercut.price_kzt == Decimal("8999")
    assert can_undercut.status == "below_kaspi_competitor"

    floor_limited = choose_initial_offer_price(
        supplier_cost_kzt=Decimal("8000"),
        minimum_profit_kzt=Decimal("1000"),
        competitor_price_kzt=Decimal("9000"),
        undercut_step_kzt=1,
    )
    assert floor_limited.price_kzt == floor_limited.safe_floor_kzt
    assert floor_limited.price_kzt > Decimal("8999")
    assert floor_limited.status == "safe_floor_above_market"


def test_ozon_match_checks_all_strategies_and_chooses_lowest_strict_price() -> None:
    product = {
        "title": "Solgar Magnesium Citrate 400 mg 120 capsules",
        "brand": "Solgar",
        "image_url": "https://resources.cdn-kaspi.kz/img/m/p/solgar.jpg",
    }
    exact_high = {
        "sku": "ozon-high",
        "title": product["title"],
        "brand": "Solgar",
        "ozon_url": "https://www.ozon.kz/product/solgar-high-111111111/",
    }
    exact_low = {
        "sku": "ozon-low",
        "title": product["title"],
        "brand": "Solgar",
        "ozon_url": "https://www.ozon.kz/product/solgar-low-222222222/",
    }

    class FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str, page: int = 1) -> dict:
            del page
            self.queries.append(query)
            rows = [exact_high] if len(self.queries) == 1 else [exact_low] if len(self.queries) == 2 else []
            return {"attempt": {"status_code": 200, "blocked": False}, "items": rows}

        def product_price_hints(self, url: str) -> dict:
            price = 3900 if "high" in url else 3500
            return {
                "ok": True,
                "cheaper_price_kzt": price,
                "cheaper_offer": {
                    "product_url": url,
                    "offer_sku": f"seller-{price}",
                    "seller_name": f"Seller {price}",
                    "delivery_days": 2,
                },
                "other_offer_count": 4,
            }

    client = FakeClient()
    result = product_discovery_runtime._best_ozon_match(
        client,
        product,
        max_queries=3,
        verifier=None,
    )
    assert len(client.queries) == 3
    assert result["best"]["sku"] == "ozon-low"
    assert result["best"]["supplier_price_kzt"] == 3500
    assert result["best"]["selection_reason"] == "lowest_price_across_strict_matches_and_sellers"
    assert result["best"]["priced_strict_candidates"] == 2
    assert result["best"]["total_supplier_offers_checked"] == 8


def test_discovery_keeps_scanning_until_complete_visual_pairs(monkeypatch) -> None:
    products = [
        {
            "master_sku": str(1000 + index),
            "title": f"Solgar Product {index}",
            "brand": "Solgar",
            "image_url": f"https://resources.cdn-kaspi.kz/img/m/p/{index}.jpg",
            "image_urls": [f"https://resources.cdn-kaspi.kz/img/m/p/{index}.jpg"],
            "kaspi_url": f"https://kaspi.kz/shop/p/solgar-{1000 + index}/",
            "price_kzt": 9000 + index,
            "rating": 4.8,
            "reviews": 120,
        }
        for index in range(20)
    ]

    class FakeSearch:
        def __init__(self, city_id):
            self.city_id = city_id

        def search(self, *args, **kwargs):
            return {"products": products, "stats": {"elapsed_ms": 12}}

        def close(self):
            return None

    class FakeResolver:
        def resolve(self):
            return object()

    class FakeClient:
        def __init__(self, profile):
            self.profile = profile

        def close(self):
            return None

    checked: list[str] = []

    def fake_match(client, product, *, max_queries, verifier):
        del client, max_queries, verifier
        checked.append(product["master_sku"])
        index = int(product["master_sku"]) - 1000
        if index < 12:
            return {"best": None, "top_candidates": [], "queries": [{"blocked": False}]}
        return {
            "best": {
                "sku": f"ozon-{index}",
                "title": product["title"],
                "ozon_url": f"https://www.ozon.kz/product/solgar-{900000000 + index}/",
                "supplier_url": f"https://www.ozon.kz/product/solgar-{900000000 + index}/",
                "supplier_price_kzt": 4000 + index,
                "image_url": f"https://ir.ozone.ru/s3/multimedia/{index}.jpg",
                "match_status": "CONFIRMED",
                "match_score": 0.95,
                "rating": 4.9,
                "reviews": 500,
            },
            "top_candidates": [],
            "queries": [{"blocked": False}],
        }

    monkeypatch.setattr(product_discovery_runtime, "KaspiProductSearch", FakeSearch)
    monkeypatch.setattr(product_discovery_runtime, "OzonSessionResolver", FakeResolver)
    monkeypatch.setattr(product_discovery_runtime, "OzonSessionHttpClient", FakeClient)
    monkeypatch.setattr(product_discovery_runtime, "_best_ozon_match", fake_match)

    result = product_discovery_runtime.discover_products(
        query="Solgar",
        city_id="196220100",
        target_new=2,
        max_kaspi_scan=20,
        max_ozon_queries=3,
        image_verify=False,
    )

    assert len(checked) == 14
    assert result["confirmed_pairs"] == 2
    assert result["matched_products_checked"] == 14
    assert [row["kaspi_product_id"] for row in result["rows"]] == ["1012", "1013"]
    assert result["rows"][0]["supplier_rating"] == 4.9
    assert result["rows"][0]["supplier_reviews"] == 500


def test_discovery_excludes_catalog_and_persists_strict_supplier_match(db_session) -> None:
    _seed_agent_account(db_session)
    db_session.add(Product(workspace_id=1, kaspi_product_id="111", merchant_sku="111_own", name="Already ours"))
    db_session.commit()
    queued = discover_product_candidates(ProductDiscoveryRequest(query="Solgar", target_new=2), db_session)
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert claim["job"]["job_type"] == "discover"
    assert "111" in claim["job"]["options"]["existing_kaspi_ids"]
    leased_job = db_session.get(ProductTestJob, claim["job"]["id"])
    assert leased_job is not None
    lease_now = product_test_api._now()
    if leased_job.lease_until.tzinfo is None:
        lease_now = lease_now.replace(tzinfo=None)
    assert (leased_job.lease_until - lease_now).total_seconds() > 1700

    completed = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="succeeded",
            result={
                "scanned": 20,
                "rows": [{
                    "kaspi_product_id": "222",
                    "merchant_sku": "222",
                    "product_name": "Solgar Magnesium",
                    "brand": "Solgar",
                    "image_url": "https://resources.cdn-kaspi.kz/img/m/p/222.jpg",
                    "product_url": "https://kaspi.kz/shop/p/solgar-222/",
                    "page_visible_price_kzt": "9000",
                    "supplier_url": "https://www.ozon.kz/product/solgar-222222222/",
                    "supplier_price_kzt": "4000",
                    "supplier_delivery_days": 3,
                    "supplier_delivery_text": "Доставим 30 августа",
                    "supplier_delivery_date": "2026-08-30",
                    "supplier_offer_sku": "ozon-222",
                    "supplier_seller_name": "Supplier",
                    "supplier_seller_rating": 4.9,
                    "supplier_seller_reviews": 132,
                    "supplier_product_title": "Solgar Magnesium Ozon",
                    "supplier_image_url": "https://ir.ozone.ru/s3/multimedia/solgar.jpg",
                    "supplier_image_urls": ["https://ir.ozone.ru/s3/multimedia/solgar.jpg"],
                    "supplier_rating": 4.8,
                    "supplier_reviews": 524,
                    "supplier_offer_count": 5,
                    "match_status": "CONFIRMED",
                    "match_score": 0.96,
                    "queries_tested": 3,
                    "priced_strict_candidates": 2,
                    "total_supplier_offers_checked": 9,
                    "offers": {},
                }],
            },
        ),
        db_session,
    )
    assert queued["queued"] is True
    assert completed["items"][0]["status"] == "ready_to_add"
    assert completed["items"][0]["offers"]["supplier"]["validated"] is True
    assert completed["items"][0]["offers"]["supplier"]["supplier_image_url"].endswith("solgar.jpg")
    assert completed["items"][0]["offers"]["supplier"]["supplier_rating"] == 4.8
    assert completed["items"][0]["offers"]["supplier"]["supplier_reviews"] == 524
    assert completed["items"][0]["offers"]["supplier"]["supplier_delivery_text"] == "Доставим 30 августа"
    assert completed["items"][0]["offers"]["supplier"]["priced_strict_candidates"] == 2
    assert completed["items"][0]["offers"]["supplier"]["total_supplier_offers_checked"] == 9


def test_manual_ozon_url_reuses_validation_job_and_refreshes_visual_pair(db_session) -> None:
    _seed_agent_account(db_session)
    item = ProductTestItem(
        workspace_id=1,
        input_reference="Solgar",
        kaspi_product_id="333333333",
        merchant_sku="333333333",
        name="Solgar Magnesium",
        brand="Solgar",
        image_url="https://resources.cdn-kaspi.kz/img/m/p/333.jpg",
        kaspi_url="https://kaspi.kz/shop/p/solgar-333333333/",
        observed_price_kzt=Decimal("9000"),
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"kaspi": {"image_urls": ["https://resources.cdn-kaspi.kz/img/m/p/333.jpg"]}},
        status="needs_supplier_link",
        active=True,
    )
    db_session.add(item)
    db_session.commit()
    url = "https://www.ozon.kz/product/solgar-magnesium-555555555/"
    queued = validate_product_supplier(
        item.id,
        product_test_api.SupplierUrlRequest(supplier_url=url),
        db_session,
    )
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert queued["job"]["job_type"] == "validate_supplier"
    assert claim["job"]["options"]["product"]["title"] == item.name
    assert claim["job"]["options"]["product"]["image_urls"] == [item.image_url]
    completed = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="succeeded",
            result={
                "supplier_url": url,
                "supplier_price_kzt": 4100,
                "supplier_offer_sku": "seller-555",
                "supplier_seller_name": "Manual seller",
                "supplier_product_title": "Solgar Magnesium Ozon",
                "supplier_image_url": "https://ir.ozone.ru/s3/multimedia/manual.jpg",
                "supplier_image_urls": ["https://ir.ozone.ru/s3/multimedia/manual.jpg"],
                "match_status": "REVIEW",
                "match_score": 0.74,
                "image_match": {"status": "SUPPORT", "score": 0.84},
                "validated": True,
            },
        ),
        db_session,
    )
    supplier = completed["item"]["offers"]["supplier"]
    assert completed["item"]["status"] == "ready_to_add"
    assert supplier["supplier_image_url"].endswith("manual.jpg")
    assert supplier["validation_source"] == "manual_url_other_offers"
    assert supplier["image_match"]["status"] == "SUPPORT"


def test_confirmed_kaspi_create_enrolls_existing_fast_dumping_atomically(db_session) -> None:
    _seed_agent_account(db_session)
    item = ProductTestItem(
        workspace_id=1,
        input_reference="Solgar",
        kaspi_product_id="333333333",
        merchant_sku="333333333",
        name="Solgar Magnesium",
        brand="Solgar",
        image_url="https://resources.cdn-kaspi.kz/img/m/p/333.jpg",
        kaspi_url="https://kaspi.kz/shop/p/solgar-333333333/",
        supplier_url="https://www.ozon.kz/product/solgar-444444444/",
        observed_price_kzt=Decimal("9000"),
        test_price_kzt=None,
        preorder_days=0,
        stock_count=5,
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"supplier": {
            "supplier_url": "https://www.ozon.kz/product/solgar-444444444/",
            "supplier_price_kzt": "4000",
            "supplier_delivery_days": 3,
            "supplier_offer_sku": "ozon-444",
            "supplier_seller_name": "Supplier",
            "validated": True,
        }},
        status="ready_to_add",
        active=True,
    )
    db_session.add(item)
    db_session.commit()
    queued = add_product_to_kaspi(item.id, db_session)
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert claim["job"]["job_type"] == "create_offer"
    assert claim["job"]["options"]["initial_price_kzt"] == 8999
    completed = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="succeeded",
            result={
                "result": "CREATED_AND_VISIBLE",
                "merchant_sku": "333333333_987654321",
                "after": {"found": True, "sku": "333333333_987654321", "price_kzt": 8999},
            },
        ),
        db_session,
    )
    product = db_session.scalar(select(Product).where(Product.kaspi_product_id == "333333333"))
    assert queued["item"]["status"] == "adding_to_kaspi"
    assert completed["item"]["status"] == "enrolled_fast_dumping"
    assert product is not None and product.merchant_sku == "333333333_987654321"
    assert db_session.scalar(select(FastDumpingPolicy).where(FastDumpingPolicy.product_id == product.id)) is not None
    assert db_session.scalar(select(FastDumpingJob).where(FastDumpingJob.product_id == product.id)) is not None
    assert db_session.scalar(select(MonitorTarget)) is not None
    assert db_session.scalar(select(BrowserAgentJob)) is not None
    assert db_session.scalar(select(SupplierOfferState)) is not None


def test_changed_supplier_url_must_be_revalidated_before_add(db_session) -> None:
    _seed_agent_account(db_session)
    original_url = "https://www.ozon.kz/product/original-444444444/"
    replacement_url = "https://www.ozon.kz/product/replacement-555555555/"
    item = ProductTestItem(
        workspace_id=1,
        input_reference="Solgar",
        kaspi_product_id="333333333",
        merchant_sku="333333333",
        name="Solgar Magnesium",
        kaspi_url="https://kaspi.kz/shop/p/solgar-333333333/",
        supplier_url=original_url,
        observed_price_kzt=Decimal("9000"),
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"supplier": {
            "supplier_url": original_url,
            "supplier_price_kzt": "4000",
            "validated": True,
        }},
        status="ready_to_add",
        active=True,
    )
    db_session.add(item)
    db_session.commit()

    updated = update_product_test_item(
        item.id,
        ProductTestUpdate(supplier_url=replacement_url),
        db_session,
    )
    assert updated["status"] == "needs_supplier_validation"
    with pytest.raises(product_test_api.HTTPException) as caught:
        add_product_to_kaspi(item.id, db_session)
    assert caught.value.status_code == 409


def test_missing_product_photo_is_prioritized_for_local_agent_and_cached(db_session) -> None:
    product = Product(
        workspace_id=1,
        kaspi_product_id="110563850",
        merchant_sku="110563850_272949101",
        name="Test existing product",
    )
    db_session.add(product)
    db_session.commit()

    first = resolve_product_image(product.id, db_session)

    assert first.image_url is None
    assert first.cached is False
    assert first.pending is True
    db_session.refresh(product)
    assert product.image_backfill_after is not None

    product.image_url = "https://resources.cdn-kaspi.kz/img/existing-product.jpg"
    db_session.commit()
    second = resolve_product_image(product.id, db_session)

    assert second.cached is True
    assert second.pending is False
    assert second.image_url == product.image_url


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
    assert "Ожидает Agent" in resolver
    for filename in ("orders.html", "products.html", "product-detail.html", "dumping.html", "fast-dumping.html"):
        page = (ROOT / "backend/app/static" / filename).read_text(encoding="utf-8")
        assert "product-image-resolver.js" in page
    photo_css = (ROOT / "backend/app/static/product-images.css").read_text(encoding="utf-8")
    assert ".product-thumb,.order-product-photo,.product-photo,.fast-product-photo,.dumping-product-photo{width:100px;height:100px" in photo_css
