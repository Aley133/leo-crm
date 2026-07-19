from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, AsyncIterator, Awaitable, Callable


@dataclass(frozen=True, slots=True)
class BrowserPageResult:
    final_url: str
    content: str
    duration_ms: int


class PlaywrightPoolError(RuntimeError):
    pass


class PlaywrightNavigationTimeout(PlaywrightPoolError):
    pass


BrowserLauncher = Callable[[], Awaitable[tuple[Any, Any]]]


class PlaywrightBrowserPool:
    """Small concrete Playwright pool for supplier-card checks.

    One Chromium browser process is reused. Every check receives a fresh isolated
    BrowserContext and Page, which are always closed before the concurrency slot
    is returned. Cookie or local-storage state is therefore never shared between
    independent MonitorTarget checks.
    """

    def __init__(
        self,
        *,
        concurrency: int = 2,
        headless: bool = True,
        launcher: BrowserLauncher | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._headless = headless
        self._launcher = launcher or self._launch_local_chromium
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._start_lock = asyncio.Lock()

    async def _launch_local_chromium(self) -> tuple[Any, Any]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightPoolError(
                "Playwright is not installed; install Python package and Chromium runtime"
            ) from exc

        playwright = await async_playwright().start()
        try:
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
                raise PlaywrightPoolError(f"Unable to start Chromium: {exc}") from exc

    async def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    @asynccontextmanager
    async def isolated_page(self) -> AsyncIterator[Any]:
        await self.start()
        async with self._semaphore:
            if self._browser is None:
                raise PlaywrightPoolError("Chromium is not available")
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
                # Ozon renders commercial data after DOMContentLoaded. A bounded
                # settle period is intentionally concrete to this browser fetch path;
                # it does not reuse context state between independent checks.
                remaining_ms = max(0, timeout_ms - int((monotonic() - started) * 1000))
                if remaining_ms:
                    await page.wait_for_timeout(min(2500, remaining_ms))
                content = await page.content()
                final_url = page.url
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
        )
