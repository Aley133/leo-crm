from __future__ import annotations

import asyncio

import pytest

from backend.app.supplier_adapters.playwright_pool import PlaywrightBrowserPool


class _Locator:
    async def inner_text(self, *, timeout: int) -> str:
        assert timeout == 1000
        return " Product ready "


class _NavigatingPage:
    def __init__(self) -> None:
        self.content_calls = 0
        self.url = "https://www.ozon.ru/product/608450235/"

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state == "domcontentloaded"
        assert timeout > 0

    async def wait_for_timeout(self, timeout: int) -> None:
        assert timeout > 0

    async def content(self) -> str:
        self.content_calls += 1
        if self.content_calls == 1:
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is navigating and changing the content."
            )
        return "<html><body>Product ready</body></html>"

    async def title(self) -> str:
        return "Ozon product"

    def locator(self, selector: str) -> _Locator:
        assert selector == "body"
        return _Locator()


class _BrokenPage(_NavigatingPage):
    async def content(self) -> str:
        raise RuntimeError("browser process crashed")


def test_capture_retries_transient_late_navigation() -> None:
    pool = PlaywrightBrowserPool()
    page = _NavigatingPage()

    content, final_url, title, body = asyncio.run(
        pool._capture_stable_page(page, started=0.0, timeout_ms=10**12)
    )

    assert page.content_calls == 2
    assert "Product ready" in content
    assert final_url.endswith("/608450235/")
    assert title == "Ozon product"
    assert body == "Product ready"


def test_capture_does_not_retry_unrelated_browser_errors() -> None:
    pool = PlaywrightBrowserPool()

    with pytest.raises(RuntimeError, match="browser process crashed"):
        asyncio.run(pool._capture_stable_page(_BrokenPage(), started=0.0, timeout_ms=10**12))
