from __future__ import annotations

import asyncio

from backend.app.supplier_adapters.playwright_pool import PlaywrightBrowserPool


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.ozon.ru/product/608450235/"
        self.closed = False

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url

    async def wait_for_timeout(self, timeout: int) -> None:
        return None

    async def content(self) -> str:
        return "<html><body>trusted Ozon session</body></html>"

    async def title(self) -> str:
        return "Ozon product"

    def locator(self, selector: str):
        return self

    async def inner_text(self, *, timeout: int) -> str:
        return "trusted Ozon session"

    async def close(self) -> None:
        self.closed = True


class SharedContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class RemoteBrowser:
    def __init__(self, context: SharedContext) -> None:
        self.contexts = [context]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


async def _exercise_remote_profile():
    context = SharedContext()
    browser = RemoteBrowser(context)
    playwright = FakePlaywright()

    async def launcher():
        return playwright, browser

    pool = PlaywrightBrowserPool(
        launcher=launcher,
        cdp_endpoint="wss://browser.example.test/devtools/browser/token",
        reuse_default_context=True,
        concurrency=1,
    )
    first = await pool.fetch_html("https://www.ozon.ru/product/1", timeout_seconds=5)
    second = await pool.fetch_html("https://www.ozon.ru/product/2", timeout_seconds=5)
    await pool.close()
    return first, second, context, browser, playwright


def test_cdp_mode_reuses_profile_but_closes_each_page_only() -> None:
    first, second, context, browser, playwright = asyncio.run(_exercise_remote_profile())

    assert first.title == "Ozon product"
    assert second.body_text == "trusted Ozon session"
    assert len(context.pages) == 2
    assert all(page.closed for page in context.pages)
    assert context.closed is False
    assert browser.closed is False
    assert playwright.stopped is True


def test_cdp_endpoint_rejects_unsafe_scheme() -> None:
    try:
        PlaywrightBrowserPool(cdp_endpoint="file:///tmp/chrome")
    except ValueError as exc:
        assert "http, https, ws or wss" in str(exc)
    else:
        raise AssertionError("unsafe CDP endpoint scheme was accepted")
