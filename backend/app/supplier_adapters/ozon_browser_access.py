from __future__ import annotations

from dataclasses import replace

from .base import AdapterRequest, NormalizedOffer
from .delivery_normalizer import DeliveryNormalizer
from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with anti-bot and delivery normalization."""

    code = "ozon-browser-v9"

    _DELIVERY_MARKERS = (
        "доставим",
        "доставка",
        "получить",
        "получение",
        "пункт ozon",
        "пункт выдачи",
        "курьер",
        "самовывоз",
    )
    _DELIVERY_EXCLUSIONS = (
        "до конца",
        "распродажа",
        "акция",
        "скидка",
        "купон",
        "осталось",
    )

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        currency = str(offer.raw_metadata.get("currency") or "").strip().upper() or None
        offer = replace(offer, currency=currency)
        if offer.delivery_days is not None:
            return offer

        # TGBAD proved that Ozon often renders the delivery promise well after the
        # structured offer data. Use the same managed browser pool, wait for delivery
        # text, trigger lazy rendering with a short scroll, and read the complete body
        # instead of PlaywrightBrowserPool's intentionally compact 1200-char snapshot.
        try:
            response = await self._fetch_delivery_page(request.url)
        except Exception:
            # Delivery enrichment is best-effort. A valid price/stock observation must
            # not become a failed monitor attempt only because Ozon hid the promise.
            return offer

        self._classify_page(response)
        delivery_days = DeliveryNormalizer.from_context(
            response.body_text,
            markers=self._DELIVERY_MARKERS,
            excluded_phrases=self._DELIVERY_EXCLUSIONS,
            window=2,
        )
        if delivery_days is None:
            return offer

        metadata = dict(offer.raw_metadata)
        metadata["delivery_source"] = "ozon_waited_full_visible_text"
        metadata["delivery_response_url"] = response.final_url
        return replace(
            offer,
            delivery_days=delivery_days,
            adapter_schema_version=self.code,
            raw_metadata=metadata,
        )

    async def _fetch_delivery_page(self, url: str) -> BrowserPageResult:
        async with self._pool.isolated_page() as page:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self._timeout_seconds * 1000),
            )
            await page.wait_for_timeout(4000)
            try:
                await page.wait_for_function(
                    """() => /Доставим|Доставка|Курьером|Пункты выдачи|постамат|завтра|сегодня/i.test(document.body.innerText || '')""",
                    timeout=min(12000, int(self._timeout_seconds * 1000)),
                )
            except Exception:
                pass

            # Ozon lazy-renders the right-hand delivery card after viewport activity.
            for _ in range(2):
                await page.mouse.wheel(0, 500)
                await page.wait_for_timeout(700)
            await page.mouse.wheel(0, -900)
            await page.wait_for_timeout(700)

            body_text = await page.locator("body").inner_text(timeout=10000)
            title = await self._pool._safe_title(page)
            content = await page.content()
            return BrowserPageResult(
                final_url=page.url,
                content=content,
                duration_ms=0,
                title=title,
                body_text=str(body_text),
            )

    @classmethod
    def _classify_page(cls, response: BrowserPageResult) -> None:
        super()._classify_page(response)
        page_text = cls._page_text(response)
        challenge_markers = (
            "challenge",
            "antibot",
            "robot check",
            "проверяем ваш браузер",
            "похоже, нет соединения",
        )
        if any(marker in page_text for marker in challenge_markers):
            raise AdapterBlockedError(
                "Ozon anti-bot challenge blocked browser access; "
                + cls._diagnostic_summary(response)
            )
