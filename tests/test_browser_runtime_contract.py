from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from backend.app.supplier_adapters.base import AccessStrategy, AdapterRequest
from backend.app.supplier_adapters.errors import (
    AdapterCaptchaError,
    AdapterNetworkError,
    AdapterTimeoutError,
)
from backend.app.supplier_adapters.ozon_browser import OzonBrowserAdapter
from backend.app.supplier_adapters.playwright_pool import (
    BrowserPageResult,
    PlaywrightBrowserPool,
    PlaywrightNavigationTimeout,
    PlaywrightPoolError,
)


class StubPool:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests: list[tuple[str, float]] = []
        self.closed = False

    async def fetch_html(self, url: str, *, timeout_seconds: float):
        self.requests.append((url, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        self.closed = True


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.ozon.ru/product/final-1/"
        self.closed = False
        self.goto_calls: list[tuple[str, str, int]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    async def content(self) -> str:
        return "<html>ok</html>"

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.contexts: list[FakeContext] = []
        self.closed = False

    async def new_context(self, **kwargs) -> FakeContext:
        page = FakePage()
        context = FakeContext(page)
        self.pages.append(page)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


async def _fake_launcher():
    return FakePlaywright(), FakeBrowser()


def test_playwright_pool_uses_fresh_context_and_always_closes_it() -> None:
    pool = PlaywrightBrowserPool(concurrency=1, launcher=_fake_launcher)

    first = asyncio.run(pool.fetch_html("https://www.ozon.ru/product/1", timeout_seconds=5))
    second = asyncio.run(pool.fetch_html("https://www.ozon.ru/product/2", timeout_seconds=5))

    browser = pool._browser
    assert first.content == "<html>ok</html>"
    assert second.final_url.endswith("final-1/")
    assert len(browser.contexts) == 2
    assert browser.contexts[0] is not browser.contexts[1]
    assert all(context.closed for context in browser.contexts)
    assert all(page.closed for page in browser.pages)

    asyncio.run(pool.close())
    assert browser.closed is True


def test_ozon_browser_adapter_implements_existing_supplier_contract() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "Product",
        "name": "Omega 3",
        "offers": {
          "price": "3734",
          "priceCurrency": "RUB",
          "availability": "https://schema.org/InStock",
          "seller": {"name": "Ozon"}
        }
      }
      </script>
    </head></html>
    """
    pool = StubPool(
        BrowserPageResult(
            final_url="https://www.ozon.ru/product/omega-3-1/",
            content=html,
            duration_ms=120,
        )
    )
    adapter = OzonBrowserAdapter(pool, timeout_seconds=17)

    offer = asyncio.run(
        adapter.fetch(
            AdapterRequest(
                supplier_product_id=17,
                url="https://www.ozon.ru/product/omega-3-1/",
                external_id="omega-3-1",
            )
        )
    )

    assert adapter.access_strategy == AccessStrategy.BROWSER
    assert offer.supplier_product_id == 17
    assert offer.price == Decimal("3734")
    assert offer.available is True
    assert offer.seller == "Ozon"
    assert offer.raw_metadata["context_isolation"] == "fresh_per_check"
    assert pool.requests == [("https://www.ozon.ru/product/omega-3-1/", 17)]


def test_ozon_browser_adapter_classifies_runtime_and_page_failures() -> None:
    request = AdapterRequest(
        supplier_product_id=17,
        url="https://www.ozon.ru/product/omega-3-1/",
        external_id="omega-3-1",
    )

    with pytest.raises(AdapterTimeoutError):
        asyncio.run(
            OzonBrowserAdapter(
                StubPool(error=PlaywrightNavigationTimeout("deadline"))
            ).fetch(request)
        )

    with pytest.raises(AdapterNetworkError):
        asyncio.run(
            OzonBrowserAdapter(
                StubPool(error=PlaywrightPoolError("chromium unavailable"))
            ).fetch(request)
        )

    with pytest.raises(AdapterCaptchaError):
        asyncio.run(
            OzonBrowserAdapter(
                StubPool(
                    BrowserPageResult(
                        final_url=request.url,
                        content="<html>Подтвердите, что вы не робот</html>",
                        duration_ms=10,
                    )
                )
            ).fetch(request)
        )
