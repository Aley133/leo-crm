import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.kaspi_xml_import import parse_kaspi_products
from backend.app.fast_dumping_agent_api import FastAgentIdentity
from backend.app.models import MarketplaceAccount, Product
from backend.app.product_images import normalize_product_image_url
from backend.app.product_registry_api import resolve_product_image
from backend.app import kaspi_product_photo
from backend.app.product_test_api import (
    ProductDiscoveryRequest,
    ProductTestNewCardRequest,
    ProductTestNewCardCategoryRequest,
    ProductTestAgentHeartbeat,
    ProductTestInspectRequest,
    ProductTestUpdate,
    ProductTestAgentIdentity,
    ProductTestAgentResult,
    add_product_to_kaspi,
    build_product_test_xml,
    claim_product_test_job,
    complete_product_test_job,
    create_product_test_new_card,
    discover_product_candidates,
    heartbeat_product_test_agent,
    inspect_product,
    map_product_test_new_card_category,
    prepare_product_test_new_card,
    read_product_test_state,
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
from tools.ozon_http.parser import parse_product_page
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
        preorder_days=0,
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
    assert availability.attrib == {"available": "yes", "storeId": "PP1", "preOrder": "1", "stockCount": "7"}
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
    test_html = (ROOT / "backend/app/static/product-test.html").read_text(encoding="utf-8")
    add_html = (ROOT / "backend/app/static/add-product.html").read_text(encoding="utf-8")
    script = (ROOT / "backend/app/static/product-test.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/product_test_agent.py").read_text(encoding="utf-8")
    fast_agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    assert "app.include_router(product_test_router)" in main
    assert '@router.get("/crm/product-test"' in ui
    assert '@router.get("/crm/add-product"' in ui
    assert 'data-product-test-page="product-test"' in test_html
    assert 'data-product-test-page="add-product"' in add_html
    assert 'id="discover-form"' in test_html
    assert 'id="discover-form"' not in add_html
    assert 'id="new-card-form"' not in test_html
    assert 'id="new-card-form"' in add_html
    assert 'id="settings-form"' in test_html
    assert 'id="settings-form"' in add_html
    assert "ЕДИНЫЙ PRODUCT TEST AGENT" in test_html
    assert "ЕДИНЫЙ PRODUCT TEST AGENT" in add_html
    assert "LEO-Product-Test-Agent.exe" in test_html
    assert "LEO-Product-Test-Agent.exe" in add_html
    assert 'class="active" href="/crm/product-test"' in test_html
    assert 'class="active" href="/crm/add-product"' in add_html
    assert "/api/product-test/discover" in script
    assert "/api/product-test/new-cards/prepare" in script
    assert "/map-category" in script
    assert "create_new_card" in script
    assert "confirm_new_card" in script
    assert "/validate-supplier" in script
    assert "/add`" in script
    assert 'name="max_undercut_gap_percent"' in test_html
    assert 'name="max_undercut_gap_percent"' in add_html
    assert 'name="delivery_price_premium_kzt"' in test_html
    assert 'name="delivery_advantage_days"' in test_html
    assert "renderAgent(payload.agent" in script
    assert "Проверить / заменить ссылку" in script
    assert "Выгрузить на Kaspi" in script
    assert "lab-results-table" in script
    assert "supplier_rating" in script
    assert "supplier_delivery_text" in script
    assert "цена карточки Ozon" in script
    assert "supplier_price_source" in script
    assert "СТАРТ KASPI" in script
    assert "preOrder ${Math.max(1" in script
    assert "точно по вашей ссылке" in script
    assert "new-card-action-message" in script
    assert 'activeJobTypes.has("map_new_card_category")' in script
    assert "Ожидаем поля категории…" in script
    assert '["discover", "validate_supplier"].includes(job.job_type)' in script
    assert 'id="kaspi-submissions-section"' in test_html
    assert 'id="kaspi-submissions-section"' in add_html
    assert "renderSubmissions(pageSubmissions)" in script
    assert 'submission.route === "new_card"' in script
    assert "isAddProductPage ? isNewCard : !isNewCard" in script
    assert "Повторить выгрузку" in script
    assert "supplier_image_url" in script
    assert 'href="/crm/monitoring"' in script
    assert "ВИЗУАЛЬНОЕ СОПОСТАВЛЕНИЕ" in test_html
    assert "НОВАЯ КАРТОЧКА KASPI" in add_html
    assert "/api/product-test-agent/claim" in agent
    assert "inspect_kaspi_product" in agent
    assert "discover_products" in agent
    assert "create_linked_offer" in agent
    assert 'job_type == "prepare_new_card"' in agent
    assert 'job_type == "confirm_new_card"' in agent
    assert "/api/product-test-agent/claim" not in fast_agent
    api = (ROOT / "backend/app/product_test_api.py").read_text(encoding="utf-8")
    assert 'status="queued"' in api
    assert "ProductTestJob.workspace_id == payload.workspace_id" in api
    assert "ProductTestItem.workspace_id == job.workspace_id" in api
    assert "Product.workspace_id == job.workspace_id" in api


def test_add_product_link_is_present_in_static_crm_navigation() -> None:
    static_dir = ROOT / "backend/app/static"
    pages = [path for path in static_dir.glob("*.html") if 'href="/crm/product-test"' in path.read_text(encoding="utf-8")]
    assert pages
    for path in pages:
        assert 'href="/crm/add-product"' in path.read_text(encoding="utf-8"), path.name


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


def test_ozon_match_uses_explicit_kzt_card_price_when_seller_modal_has_none() -> None:
    product = {
        "title": "Solgar Magnesium Citrate 400 mg 120 capsules",
        "brand": "Solgar",
    }
    candidate = {
        "sku": "ozon-card-price",
        "title": product["title"],
        "brand": "Solgar",
        "ozon_url": "https://www.ozon.kz/product/solgar-magnesium-222222222/",
        "price_kzt": 3800,
        "effective_price_kzt": 3650,
        "delivery_days": 4,
        "delivery_text": "Доставим через 4 дня",
        "image_url": "https://ir.ozone.ru/s3/multimedia/solgar.jpg",
    }

    class FakeClient:
        def search(self, query: str, page: int = 1) -> dict:
            del query, page
            return {"attempt": {"status_code": 200, "blocked": False}, "items": [candidate]}

        def product_price_hints(self, url: str) -> dict:
            del url
            return {
                "ok": True,
                "cheaper_price_kzt": None,
                "cheaper_offer": None,
                "other_offer_count": 0,
            }

    result = product_discovery_runtime._best_ozon_match(
        FakeClient(),
        product,
        max_queries=1,
        verifier=None,
    )

    assert result["best"]["supplier_price_kzt"] == 3650
    assert result["best"]["supplier_price_source"] == "search_card.effective_price_kzt"
    assert result["best"]["supplier_delivery_days"] == 4
    assert result["best"]["priced_strict_candidates"] == 1


def test_ozon_card_price_fallback_never_uses_rub_amount() -> None:
    candidate = {
        "ozon_url": "https://www.ozon.kz/product/example-222222222/",
        "price_value": 799,
        "price_rub": 799,
        "currency_code": "RUB",
    }

    assert product_discovery_runtime._attach_search_card_supplier_offer(candidate) is False
    assert "supplier_price_kzt" not in candidate


def test_exact_product_page_parser_prefers_final_kzt_price_over_instalment() -> None:
    payload = {
        "widgetStates": {
            "webPrice-1507555262-default-1": (
                '{"finalPrice":"2 250 ₸","originalPrice":"11 430 ₸",'
                '"installment":{"text":"188 ₸ × 12 месяцев"}}'
            ),
            "webProductHeading-1507555262-default-1": '{"title":"GLS Пивные дрожжи 120 капсул"}',
            "webGallery-1507555262-default-1": '{"coverImage":"https://ir.ozone.ru/s3/multimedia/yeast.jpg"}',
            "webRecommendations-price": '{"finalPrice":"999 ₽"}',
        }
    }

    parsed = parse_product_page(payload, expected_currency="KZT")

    assert parsed["price_kzt"] == 2250
    assert parsed["currency_code"] == "KZT"
    assert parsed["widget_key"].startswith("webPrice-")
    assert "finalPrice" in parsed["price_source"]
    assert parsed["title"] == "GLS Пивные дрожжи 120 капсул"
    assert parsed["image_url"].endswith("yeast.jpg")


def test_exact_product_page_prefers_delivery_date_over_zero_tenge_today() -> None:
    payload = {
        "widgetStates": {
            "webPrice-484053304-default-1": (
                '{"finalPrice":"3 644 ₸","installment":{"text":"0 ₸ сегодня"}}'
            ),
            "webAddToCart-484053304-default-1": (
                '{"buttonText":"0 ₸ сегодня · В корзину",'
                '"deliveryText":"Доставим 2 сентября"}'
            ),
        }
    }

    parsed = parse_product_page(
        payload,
        expected_currency="KZT",
        today=date(2026, 8, 29),
    )

    assert parsed["price_kzt"] == 3644
    assert parsed["delivery_text"] == "Доставим 2 сентября"
    assert parsed["delivery_date"] == "2026-09-02"
    assert parsed["delivery_days"] == 4


def test_ozon_match_uses_exact_product_page_when_modal_and_search_price_are_empty() -> None:
    candidate = {
        "sku": "1507555262",
        "title": "GLS Pharmaceuticals Пивные дрожжи 120 капсул",
        "brand": "GLS Pharmaceuticals",
        "ozon_url": "https://www.ozon.kz/product/pivnye-drozhzhi-1507555262/",
        "image_url": "https://ir.ozone.ru/s3/multimedia/yeast.jpg",
    }

    class FakeClient:
        def search(self, query: str, page: int = 1) -> dict:
            del query, page
            return {"attempt": {"status_code": 200, "blocked": False}, "items": [candidate]}

        def product_price_hints(self, url: str) -> dict:
            del url
            return {"ok": True, "cheaper_price_kzt": None, "cheaper_offer": None, "other_offer_count": 0}

        def product_page_price(self, url: str, product_id: str) -> dict:
            assert url == candidate["ozon_url"]
            assert product_id == candidate["sku"]
            return {
                "ok": True,
                "product_id": product_id,
                "price_kzt": 2250,
                "price_source": "webPrice-1507555262.finalPrice",
                "delivery_days": 1,
            }

    result = product_discovery_runtime._best_ozon_match(
        FakeClient(),
        {"title": candidate["title"], "brand": candidate["brand"]},
        max_queries=1,
        verifier=None,
    )

    assert result["best"]["supplier_price_kzt"] == 2250
    assert result["best"]["supplier_price_source"].startswith("product_page.webPrice-")
    assert result["best"]["supplier_delivery_days"] == 1


def test_manual_ozon_url_uses_only_exact_product_page_price_and_delivery(monkeypatch) -> None:
    url = "https://www.ozon.kz/product/solgar-magnesium-555555555/"

    class FakeResolver:
        def resolve(self):
            return object()

    class FakeClient:
        def __init__(self, profile):
            self.profile = profile

        def product_price_hints(self, product_url: str) -> dict:
            raise AssertionError(f"manual URL must not inspect other sellers: {product_url}")

        def search(self, query: str, page: int = 1) -> dict:
            raise AssertionError(f"manual URL must not start a search: {query} {page}")

        def product_page_price(self, product_url: str, product_id: str) -> dict:
            assert product_url == url
            assert product_id == "555555555"
            return {
                "ok": True,
                "product_id": product_id,
                "price_kzt": 2250,
                "price_source": "webPrice-555555555.finalPrice",
                "delivery_text": "Доставим завтра",
                "delivery_days": 1,
                "card": {
                    "title": "Solgar Magnesium",
                    "image_url": "https://ir.ozone.ru/s3/multimedia/manual.jpg",
                    "image_urls": ["https://ir.ozone.ru/s3/multimedia/manual.jpg"],
                    "rating": 4.9,
                    "reviews": 120,
                },
            }

        def close(self):
            return None

    monkeypatch.setattr(product_discovery_runtime, "OzonSessionResolver", FakeResolver)
    monkeypatch.setattr(product_discovery_runtime, "OzonSessionHttpClient", FakeClient)

    result = product_discovery_runtime.validate_supplier_url(url)

    assert result["supplier_url"] == url
    assert result["supplier_price_kzt"] == 2250
    assert result["supplier_price_source"] == "manual_product_page.webPrice-555555555.finalPrice"
    assert result["supplier_delivery_days"] == 1
    assert result["supplier_delivery_text"] == "Доставим завтра"
    assert result["match_status"] == "OPERATOR_CONFIRMED"
    assert result["manual_override"] is True
    assert result["validated"] is True


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
                    "supplier_price_source": "search_card.effective_price_kzt",
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
    assert completed["items"][0]["offers"]["supplier"]["supplier_price_source"] == "search_card.effective_price_kzt"
    assert completed["items"][0]["offers"]["supplier"]["validation_source"] == "strict_multimodal_card_price_fallback"
    assert completed["items"][0]["offers"]["supplier"]["priced_strict_candidates"] == 2
    assert completed["items"][0]["offers"]["supplier"]["total_supplier_offers_checked"] == 9


def test_new_discovery_replaces_candidates_but_keeps_kaspi_submissions(db_session) -> None:
    _seed_agent_account(db_session)
    old_candidate = ProductTestItem(
        workspace_id=1,
        input_reference="old",
        kaspi_product_id="710000001",
        merchant_sku="710000001",
        name="Old candidate",
        kaspi_url="https://kaspi.kz/shop/p/old-710000001/",
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={},
        status="needs_supplier_link",
        active=True,
    )
    waiting = ProductTestItem(
        workspace_id=1,
        input_reference="old",
        kaspi_product_id="710000002",
        merchant_sku="710000002",
        name="Already submitted",
        kaspi_url="https://kaspi.kz/shop/p/waiting-710000002/",
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"kaspi_submission": {"status": "waiting", "attempt": 1}},
        status="adding_to_kaspi",
        active=True,
    )
    db_session.add_all([old_candidate, waiting])
    db_session.commit()

    discover_product_candidates(ProductDiscoveryRequest(query="new batch", target_new=10), db_session)
    state = read_product_test_state(db_session)

    db_session.refresh(old_candidate)
    db_session.refresh(waiting)
    assert old_candidate.active is False
    assert waiting.active is True
    assert state["items"] == []
    assert [row["id"] for row in state["submissions"]] == [waiting.id]


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
                "supplier_price_source": "manual_product_page.webPrice-555555555.finalPrice",
                "supplier_offer_sku": "seller-555",
                "supplier_seller_name": "Manual seller",
                "supplier_product_title": "Solgar Magnesium Ozon",
                "supplier_image_url": "https://ir.ozone.ru/s3/multimedia/manual.jpg",
                "supplier_image_urls": ["https://ir.ozone.ru/s3/multimedia/manual.jpg"],
                "match_status": "OPERATOR_CONFIRMED",
                "match_score": 1.0,
                "image_match": {"status": "OPERATOR_CONFIRMED"},
                "manual_override": True,
                "visual_review_required": False,
                "validated": True,
            },
        ),
        db_session,
    )
    supplier = completed["item"]["offers"]["supplier"]
    assert completed["item"]["status"] == "ready_to_add"
    assert completed["item"]["test_price_kzt"] == Decimal("8999")
    assert completed["item"]["preorder_days"] == 1
    assert supplier["supplier_image_url"].endswith("manual.jpg")
    assert supplier["validation_source"] == "manual_exact_product_page"
    assert supplier["supplier_price_source"].startswith("manual_product_page.")
    assert supplier["image_match"]["status"] == "OPERATOR_CONFIRMED"


def test_confirmed_kaspi_create_enrolls_existing_fast_dumping_atomically(db_session, monkeypatch) -> None:
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
    assert claim["job"]["options"]["preorder_days"] == 4
    assert queued["item"]["status"] == "adding_to_kaspi"
    assert queued["item"]["offers"]["kaspi_submission"]["status"] == "waiting"
    waiting_state = read_product_test_state(db_session)
    assert waiting_state["items"] == []
    assert waiting_state["submissions"][0]["id"] == item.id
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
                "after": {
                    "found": True,
                    "sku": "333333333_987654321",
                    "price_kzt": 8999,
                    "stock_count": 5,
                    "preorder_days": 4,
                },
            },
        ),
        db_session,
    )
    product = db_session.scalar(select(Product).where(Product.kaspi_product_id == "333333333"))
    assert completed["item"]["status"] == "enrolled_fast_dumping"
    assert completed["item"]["offers"]["kaspi_submission"]["status"] == "succeeded"
    assert product is not None and product.merchant_sku == "333333333_987654321"
    assert db_session.scalar(select(FastDumpingPolicy).where(FastDumpingPolicy.product_id == product.id)) is not None
    assert db_session.scalar(select(FastDumpingJob).where(FastDumpingJob.product_id == product.id)) is not None
    assert db_session.scalar(select(MonitorTarget)) is not None
    assert db_session.scalar(select(BrowserAgentJob)) is not None
    assert db_session.scalar(select(SupplierOfferState)) is not None
    visible_state = read_product_test_state(db_session)
    assert visible_state["submissions"][0]["id"] == item.id
    hide_after = datetime.fromisoformat(completed["item"]["offers"]["kaspi_submission"]["hide_after"])
    monkeypatch.setattr(product_test_api, "_now", lambda: hide_after + timedelta(seconds=1))
    assert read_product_test_state(db_session)["submissions"] == []


def test_zero_day_kaspi_offer_is_not_enrolled_as_a_product(db_session) -> None:
    item = ProductTestItem(
        workspace_id=1,
        input_reference="GLS Omega-3",
        kaspi_product_id="138791468",
        merchant_sku="138791468",
        name="GLS Omega-3",
        kaspi_url="https://kaspi.kz/shop/p/gls-omega-3-138791468/",
        supplier_url="https://www.ozon.kz/product/omega-1521854115/",
        test_price_kzt=Decimal("9599"),
        preorder_days=1,
        stock_count=5,
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"supplier": {
            "supplier_url": "https://www.ozon.kz/product/omega-1521854115/",
            "supplier_price_kzt": "8000",
            "validated": True,
        }},
        status="adding_to_kaspi",
        active=True,
    )
    job = ProductTestJob(
        workspace_id=1,
        job_type="create_offer",
        item_id=None,
        input_reference="item:pending",
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        status="leased",
    )
    db_session.add_all([item, job])
    db_session.flush()
    job.item_id = item.id
    db_session.commit()

    with pytest.raises(ValueError, match="предзаказ 1 дн"):
        product_test_api._enroll_created_product(
            db_session,
            job=job,
            result={
                "result": "ALREADY_EXISTS",
                "merchant_sku": "138791468_857843219",
                "after": {
                    "found": True,
                    "sku": "138791468_857843219",
                    "price_kzt": 9599,
                    "stock_count": 5,
                    "preorder_days": 0,
                },
            },
        )

    assert db_session.scalar(select(Product).where(Product.kaspi_product_id == "138791468")) is None


def test_failed_kaspi_submission_stays_visible_and_can_be_retried(db_session) -> None:
    _seed_agent_account(db_session)
    supplier_url = "https://www.ozon.kz/product/solgar-444444444/"
    item = ProductTestItem(
        workspace_id=1,
        input_reference="Solgar",
        kaspi_product_id="720000001",
        merchant_sku="720000001",
        name="Solgar retry",
        kaspi_url="https://kaspi.kz/shop/p/solgar-720000001/",
        supplier_url=supplier_url,
        observed_price_kzt=Decimal("9000"),
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"supplier": {"supplier_url": supplier_url, "supplier_price_kzt": "4000", "validated": True}},
        status="ready_to_add",
        active=True,
    )
    db_session.add(item)
    db_session.commit()
    add_product_to_kaspi(item.id, db_session)
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    assert claim["job"]["options"]["preorder_days"] == 1
    failed = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="failed",
            error_code="kaspi_offer_not_visible",
            error_message="Kaspi не обнаружил товар",
        ),
        db_session,
    )

    assert failed["status"] == "failed"
    state = read_product_test_state(db_session)
    assert state["submissions"][0]["offers"]["kaspi_submission"]["status"] == "failed"
    assert state["submissions"][0]["offers"]["kaspi_submission"]["hide_after"] is None
    retried = add_product_to_kaspi(item.id, db_session)
    assert retried["item"]["offers"]["kaspi_submission"]["status"] == "waiting"
    assert retried["item"]["offers"]["kaspi_submission"]["attempt"] == 2


def test_category_rejection_disappears_from_kaspi_submissions_after_three_minutes(db_session, monkeypatch) -> None:
    _seed_agent_account(db_session)
    supplier_url = "https://www.ozon.kz/product/category-blocked-444444444/"
    item = ProductTestItem(
        workspace_id=1,
        input_reference="Blocked category",
        kaspi_product_id="720000002",
        merchant_sku="720000002",
        name="Blocked category product",
        kaspi_url="https://kaspi.kz/shop/p/blocked-720000002/",
        supplier_url=supplier_url,
        observed_price_kzt=Decimal("9000"),
        city_id="196220100",
        zone_id="Magnum_ZONE1",
        offers_json={"supplier": {"supplier_url": supplier_url, "supplier_price_kzt": "4000", "validated": True}},
        status="ready_to_add",
        active=True,
    )
    db_session.add(item)
    db_session.commit()
    add_product_to_kaspi(item.id, db_session)
    claim = claim_product_test_job(
        ProductTestAgentIdentity(agent_id="agent-w1", agent_kind="product_test", workspace_id=1, merchant_uid="merchant-1"),
        db_session,
    )
    failed_at = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(product_test_api, "_now", lambda: failed_at)
    complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="failed",
            error_code="RuntimeError",
            error_message="VALIDATE_CHOOSE_REJECTED",
        ),
        db_session,
    )

    submission = read_product_test_state(db_session)["submissions"][0]["offers"]["kaspi_submission"]
    assert submission["terminal_rejection"] is True
    assert datetime.fromisoformat(submission["hide_after"]) == failed_at + timedelta(minutes=3)
    monkeypatch.setattr(product_test_api, "_now", lambda: failed_at + timedelta(minutes=3, seconds=1))
    assert read_product_test_state(db_session)["submissions"] == []


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


def test_new_card_route_waits_for_moderation_then_uses_existing_enrollment(db_session) -> None:
    _seed_agent_account(db_session)
    identity = ProductTestAgentIdentity(
        agent_id="product-test-w1",
        agent_kind="product_test",
        workspace_id=1,
        merchant_uid="merchant-1",
    )
    supplier_url = "https://www.ozon.kz/product/new-solgar-900000001/"
    queued = prepare_product_test_new_card(
        ProductTestNewCardRequest(supplier_url=supplier_url),
        db_session,
    )
    claim = claim_product_test_job(identity, db_session)
    assert queued["job"]["job_type"] == "prepare_new_card"
    assert claim["job"]["job_type"] == "prepare_new_card"

    prepared = complete_product_test_job(
        claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=claim["job"]["lease_token"],
            status="succeeded",
            result={
                "draft": {
                    "source_url": supplier_url,
                    "sku": "900000001",
                    "title": "Solgar Test Product 60 capsules",
                    "brand": "Solgar",
                    "description": "Подробное описание товара по данным производителя. " * 3,
                    "weight": "0.25",
                    "category": "Master - Vitamins",
                    "category_title": "Витамины и БАД",
                    "category_hint": "Витамины и БАД",
                    "categories": [{"code": "Master - Vitamins", "title": "Витамины и БАД"}],
                    "attributes": [
                        {
                            "code": "vitamins*country",
                            "title": "Страна производства",
                            "required": True,
                            "value": "США",
                        }
                    ],
                    "characteristics": [{"name": "Страна производства", "value": "США"}],
                    "images": ["https://ir.ozone.ru/s3/multimedia/new-card.webp"],
                    "validation_errors": [],
                },
                "supplier": {
                    "supplier_url": supplier_url,
                    "supplier_price_kzt": 4200,
                    "supplier_price_source": "manual_product_page.webPrice.finalPrice",
                    "supplier_delivery_days": 2,
                    "supplier_delivery_text": "Доставим через 2 дня",
                    "supplier_offer_sku": "900000001",
                    "supplier_seller_name": "Ozon",
                    "supplier_product_title": "Solgar Test Product 60 capsules",
                    "supplier_image_url": "https://ir.ozone.ru/s3/multimedia/new-card.webp",
                    "supplier_image_urls": ["https://ir.ozone.ru/s3/multimedia/new-card.webp"],
                    "price_confirmed": True,
                    "validated": True,
                },
            },
        ),
        db_session,
    )
    item_id = prepared["item"]["id"]
    assert prepared["item"]["status"] == "new_card_ready"
    assert prepared["item"]["preorder_days"] == 3
    assert read_product_test_state(db_session)["new_cards"][0]["id"] == item_id

    mapping = map_product_test_new_card_category(
        item_id,
        ProductTestNewCardCategoryRequest(category="Master - Vitamins"),
        db_session,
    )
    assert mapping["job"]["job_type"] == "map_new_card_category"
    with pytest.raises(HTTPException, match="ещё загружает поля категории"):
        create_product_test_new_card(item_id, db_session)

    mapping_claim = claim_product_test_job(identity, db_session)
    remapped = complete_product_test_job(
        mapping_claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=mapping_claim["job"]["lease_token"],
            status="succeeded",
            result={
                "category": "Master - Vitamins",
                "attributes": [
                    {
                        "code": "vitamins*country",
                        "title": "Страна производства",
                        "required": True,
                        "value": "США",
                    }
                ],
            },
        ),
        db_session,
    )
    assert remapped["item"]["status"] == "new_card_ready"

    create_product_test_new_card(item_id, db_session)
    create_claim = claim_product_test_job(identity, db_session)
    assert create_claim["job"]["job_type"] == "create_new_card"
    assert create_claim["job"]["options"]["draft"]["sku"] == "900000001"
    imported = complete_product_test_job(
        create_claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=create_claim["job"]["lease_token"],
            status="succeeded",
            result={
                "result": "NEW_CARD_ACCEPTED_FOR_MODERATION",
                "import_code": "import-900000001",
                "sku": "900000001",
                "detailed_ok": True,
                "errors": 0,
            },
        ),
        db_session,
    )
    assert imported["item"]["status"] == "new_card_moderation"
    assert imported["item"]["offers"]["kaspi_submission"]["status"] == "waiting"
    waiting_state = read_product_test_state(db_session)
    assert waiting_state["new_cards"] == []
    assert waiting_state["submissions"][0]["id"] == item_id
    assert claim_product_test_job(identity, db_session)["job"] is None

    first_check = db_session.scalar(
        select(ProductTestJob).where(
            ProductTestJob.item_id == item_id,
            ProductTestJob.job_type == "confirm_new_card",
            ProductTestJob.status == "queued",
        )
    )
    first_check.lease_until = product_test_api._now() - timedelta(seconds=1)
    db_session.commit()
    moderation_claim = claim_product_test_job(identity, db_session)
    pending = complete_product_test_job(
        moderation_claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=moderation_claim["job"]["lease_token"],
            status="succeeded",
            result={"result": "NEW_CARD_PENDING_MODERATION", "official_sku": "900000001"},
        ),
        db_session,
    )
    assert pending["followup"]["status"] == "queued"

    retry_check = db_session.get(ProductTestJob, pending["followup"]["id"])
    retry_check.lease_until = product_test_api._now() - timedelta(seconds=1)
    db_session.commit()
    retry_claim = claim_product_test_job(identity, db_session)
    transient = complete_product_test_job(
        retry_claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=retry_claim["job"]["lease_token"],
            status="failed",
            error_code="AdapterNetworkError",
            error_message="temporary Merchant Cabinet timeout",
        ),
        db_session,
    )
    assert transient["retry_job"]["status"] == "queued"
    db_session.refresh(db_session.get(ProductTestItem, item_id))
    assert db_session.get(ProductTestItem, item_id).status == "new_card_moderation"

    final_check = db_session.get(ProductTestJob, transient["retry_job"]["id"])
    final_check.lease_until = product_test_api._now() - timedelta(seconds=1)
    db_session.commit()
    final_claim = claim_product_test_job(identity, db_session)
    item = db_session.get(ProductTestItem, item_id)
    enrolled = complete_product_test_job(
        final_claim["job"]["id"],
        ProductTestAgentResult(
            agent_id=identity.agent_id,
            workspace_id=1,
            lease_token=final_claim["job"]["lease_token"],
            status="succeeded",
            result={
                "result": "CREATED_AND_VISIBLE",
                "official_sku": "900000001",
                "new_card_master_sku": "880000001",
                "merchant_sku": "880000001_900000001",
                "after": {
                    "found": True,
                    "sku": "880000001_900000001",
                    "price_kzt": int(item.test_price_kzt),
                    "stock_count": item.stock_count,
                    "preorder_days": item.preorder_days,
                },
            },
        ),
        db_session,
    )
    product = db_session.scalar(select(Product).where(Product.kaspi_product_id == "880000001"))
    assert enrolled["item"]["status"] == "enrolled_fast_dumping"
    assert product is not None and product.merchant_sku == "880000001_900000001"
    assert db_session.scalar(select(MonitorTarget).where(MonitorTarget.workspace_id == 1)) is not None
    assert db_session.scalar(select(FastDumpingPolicy).where(FastDumpingPolicy.product_id == product.id)) is not None
    assert db_session.scalar(select(FastDumpingJob).where(FastDumpingJob.product_id == product.id)) is not None
