from __future__ import annotations

import asyncio
import httpx
import pytest
from tools import kaspi_fast_dumping_scanner as scanner


@pytest.fixture(autouse=True)
def clean_routes():
    scanner._PRODUCT_URLS.clear()
    yield
    scanner._PRODUCT_URLS.clear()


def test_resolved_route_skips_fallback_but_prices_and_offers_stay_fresh(monkeypatch):
    requests = []
    scans = 0
    def handler(request):
        nonlocal scans
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"offers":[
                {"merchantId":"own","merchantName":"Own","price":110},
                {"merchantId":"other","merchantName":"Other","price":100 + scans},
            ]})
        if request.url.path != "/shop/p/123456/":
            return httpx.Response(404)
        scans += 1
        return httpx.Response(200, text=f'<script>{{"promoConditions":{{"brand":"Brand","categoryCodes":[]}}}}</script><span class="item__price-once">{100 + scans} ₸</span>')
    real_client = httpx.AsyncClient
    clients = []
    def factory(**kwargs):
        client = real_client(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client
    monkeypatch.setattr(scanner.httpx, "AsyncClient", factory)
    async def run():
        async with scanner.scanner_session():
            kwargs = dict(kaspi_product_id="123456", own_merchant_id="own", city_id="city", zone_id="zone", product_name_hint="Wrong name")
            first = await scanner.scan_kaspi_competitors(**kwargs)
            first_count = len(requests)
            second = await scanner.scan_kaspi_competitors(**kwargs)
            assert len(requests) - first_count == 2
            assert second.page_visible_price_kzt != first.page_visible_price_kzt
            assert second.offers != first.offers
    asyncio.run(run())
    assert len(clients) == 1
    assert clients[0].is_closed
    assert scanner._SCANNER_CLIENT.get() is None
    assert len(requests) == 6  # First: 3 GET + POST. Next: exact GET + POST.


def test_stale_route_is_evicted_and_wrong_card_is_not_used():
    scanner._remember_product_url(("123456", "city"), "https://kaspi.kz/shop/p/stale-123456/?c=city")
    requests = []
    def handler(request):
        requests.append(request)
        if "stale" in request.url.path:
            return httpx.Response(302, headers={"Location":"https://kaspi.kz/shop/p/wrong-987654/"})
        return httpx.Response(200, text='"promoConditions":{"categoryCodes":[]}')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            page, _, url = await scanner._open_product_page(client, master_id="123456", city_id="city", product_name_hint="Correct")
            assert "correct-123456" in url
    asyncio.run(run())
    assert len(requests) == 3
    assert "correct-123456" in scanner._PRODUCT_URLS[("123456", "city")]


def test_route_cache_is_bounded_and_separates_cities():
    for index in range(1100):
        scanner._remember_product_url((str(index), "city"), f"https://kaspi.kz/shop/p/{index}/")
    assert len(scanner._PRODUCT_URLS) == 1024
    assert ("0", "city") not in scanner._PRODUCT_URLS
    scanner._remember_product_url(("1099", "other-city"), "https://kaspi.kz/shop/p/1099/?c=other-city")
    assert scanner._PRODUCT_URLS[("1099", "city")] != scanner._PRODUCT_URLS[("1099", "other-city")]


@pytest.mark.parametrize("page_size,complete", [(4, True), (5, False)])
def test_market_coverage_is_false_when_page_limit_is_exhausted(
    monkeypatch, page_size, complete
):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "merchantId": "own" if i == 0 else f"seller-{i}",
                            "merchantName": f"Seller {i}",
                            "price": 100 + i,
                        }
                        for i in range(page_size)
                    ]
                },
            )
        return httpx.Response(
            200,
            text='<script>{"promoConditions":{"categoryCodes":[]}}</script><span class="item__price-once">100 ₸</span>',
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        scanner.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    snapshot = asyncio.run(
        scanner.scan_kaspi_competitors(
            kaspi_product_id="123456",
            own_merchant_id="own",
            city_id="city",
            zone_id="zone",
            max_pages=1,
        )
    )
    assert snapshot.offers_complete is complete
