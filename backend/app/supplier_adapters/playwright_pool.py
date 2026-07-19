from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class BrowserPageResult:
    final_url: str
    content: str
    duration_ms: int
    title: str = ""
    body_text: str = ""


class PlaywrightPoolError(RuntimeError):
    pass


class PlaywrightNavigationTimeout(PlaywrightPoolError):
    pass


BrowserLauncher = Callable[[], Awaitable[tuple[Any, Any]]]


class PlaywrightBrowserPool:
    """Concrete Playwright pool for supplier-card checks.

    Local mode reuses one Chromium process while every check receives a fresh
    isolated BrowserContext. CDP mode attaches to an already running Chrome and
    intentionally reuses its existing default context so cookies, browser profile
    and trusted session state survive between checks. A new Page is still created
    and always closed for every MonitorTarget execution.
    """

    def __init__(
        self,
        *,
        concurrency: int = 2,
        headless: bool = True,
        launcher: BrowserLauncher | None = None,
        cdp_endpoint: str | None = None,
        reuse_default_context: bool | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        configured_endpoint = (cdp_endpoint or os.getenv("OZON_CDP_ENDPOINT") or "").strip()
        if configured_endpoint:
            scheme = urlparse(configured_endpoint).scheme.casefold()
            if scheme not in {"http", "https", "ws", "wss"}:
                raise ValueError("OZON_CDP_ENDPOINT must use http, https, ws or wss")

        self._semaphore = asyncio.Semaphore(concurrency)
        self._headless = headless
        self._cdp_endpoint = configured_endpoint or None
        self._launcher = launcher or self._start_configured_browser
        self._reuse_default_context = (
            bool(self._cdp_endpoint) if reuse_default_context is None else reuse_default_context
        )
        self._owns_browser = not self._reuse_default_context
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._start_lock = asyncio.Lock()

    async def _start_configured_browser(self) -> tuple[Any, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightPoolError(
                "Playwright is not installed; install Python package and Chromium runtime"
            ) from exc

        playwright = await async_playwright().start()
        try:
            if self._cdp_endpoint:
                browser = await playwright.chromium.connect_over_cdp(self._cdp_endpoint)
            else:
                browser = await playwright.chromium.launch(
                    headless=self._headless,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
        except Exception:
            await playwright.stop()
            raise
        return playwright, browser

    async def start(self) -> None:
        if self._browser is not None:
            return
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                self._playwright, self._browser = await self._launcher()
            except PlaywrightPoolError:
                raise
            except Exception as exc:
                mode = "remote Chrome session" if self._cdp_endpoint else "Chromium"
                raise PlaywrightPoolError(f"Unable to start {mode}: {exc}") from exc

    async def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        # Never terminate an externally managed Chrome process. Stopping Playwright
        # disconnects the CDP client while leaving the user's profile/session alive.
        if browser is not None and self._owns_browser:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    @asynccontextmanager
    async def isolated_page(self) -> AsyncIterator[Any]:
        await self.start()
        async with self._semaphore:
            if self._browser is None:
                raise PlaywrightPoolError("Chromium is not available")

            if self._reuse_default_context:
                contexts = list(self._browser.contexts)
                if not contexts:
                    raise PlaywrightPoolError(
                        "CDP Chrome has no default browser context; open Chrome with a user profile"
                    )
                context = contexts[0]
                page = await context.new_page()
                try:
                    yield page
                finally:
                    await page.close()
                return

            context = await self._browser.new_context(
                locale="ru-RU",
                timezone_id="Asia/Almaty",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            try:
                yield page
            finally:
                await page.close()
                await context.close()

    @staticmethod
    async def _safe_title(page: Any) -> str:
        try:
            return str(await page.title()).strip()[:300]
        except Exception:
            return ""

    @staticmethod
    async def _safe_body_text(page: Any) -> str:
        try:
            locator = page.locator("body")
            text = await locator.inner_text(timeout=1000)
            return " ".join(str(text).split())[:1200]
        except Exception:
            return ""

    async def fetch_html(self, url: str, *, timeout_seconds: float) -> BrowserPageResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = monotonic()
        timeout_ms = int(timeout_seconds * 1000)
        try:
            async with self.isolated_page() as page:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                remaining_ms = max(0, timeout_ms - int((monotonic() - started) * 1000))
                if remaining_ms:
                    await page.wait_for_timeout(min(2500, remaining_ms))
                content = await page.content()
                final_url = page.url
                title = await self._safe_title(page)
                body_text = await self._safe_body_text(page)
        except asyncio.TimeoutError as exc:
            raise PlaywrightNavigationTimeout("Browser navigation timed out") from exc
        except Exception as exc:
            if exc.__class__.__name__ == "TimeoutError":
                raise PlaywrightNavigationTimeout("Browser navigation timed out") from exc
            if isinstance(exc, PlaywrightPoolError):
                raise
            raise PlaywrightPoolError(f"Browser navigation failed: {exc}") from exc

        return BrowserPageResult(
            final_url=final_url,
            content=content,
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            title=title,
            body_text=body_text,
        )
