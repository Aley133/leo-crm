from __future__ import annotations

import re
from dataclasses import replace

from .base import AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .ozon_delivery import OzonDeliveryExtractor
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with anti-bot and observable delivery semantics."""

    code = "ozon-browser-v11"
    _DIAGNOSTIC_LIMIT = 4000

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        currency = str(offer.raw_metadata.get("currency") or "").strip().upper() or None
        base_delivery_days = offer.delivery_days
        metadata = dict(offer.raw_metadata)
        metadata["base_delivery_days"] = base_delivery_days
        offer = replace(offer, currency=currency, raw_metadata=metadata)

        # Ozon structured payloads may contain unrelated commercial timing values.
        # Therefore visible Ozon delivery semantics are authoritative whenever the
        # managed browser can read them. The base value is preserved for diagnostics.
        try:
            response = await self._fetch_delivery_page(request.url)
        except Exception as exc:
            metadata = dict(offer.raw_metadata)
            metadata["delivery_diagnostic_status"] = "delivery_page_unavailable"
            metadata["delivery_diagnostic_error"] = type(exc).__name__
            return replace(offer, raw_metadata=metadata)

        self._classify_page(response)
        raw_text = str(response.body_text or "")
        semantic_delivery_days = OzonDeliveryExtractor.from_text(raw_text)

        metadata = dict(offer.raw_metadata)
        metadata["semantic_delivery_days"] = semantic_delivery_days
        metadata["delivery_response_url"] = response.final_url
        metadata["delivery_input_length"] = len(raw_text)
        metadata["delivery_context"] = self._delivery_context(raw_text)
        metadata["delivery_diagnostic_status"] = (
            "semantic_match" if semantic_delivery_days is not None else "semantic_not_found"
        )

        if semantic_delivery_days is not None:
            metadata["delivery_source"] = "ozon_trusted_delivery_semantics"
            return replace(
                offer,
                delivery_days=semantic_delivery_days,
                adapter_schema_version=self.code,
                raw_metadata=metadata,
            )

        # Do not erase a structured value when semantic enrichment is unavailable,
        # but retain full diagnostics so the next investigation is evidence-based.
        metadata["delivery_source"] = (
            "structured_fallback" if base_delivery_days is not None else "not_found"
        )
        return replace(
            offer,
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
    def _delivery_context(cls, text: str) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return ""
        markers = tuple(
            marker
            for marker in (
                "доставим",
                "доставка",
                "доставят",
                "привезем",
                "привезём",
                "получите",
                "пункт выдачи",
                "пункт ozon",
                "постамат",
                "самовывоз",
                "0 ₸ сегодня",
            )
            if (marker_pos := compact.casefold().find(marker)) >= 0
        )
        if not markers:
            return compact[: cls._DIAGNOSTIC_LIMIT]
        positions = [compact.casefold().find(marker) for marker in markers]
        start = max(0, min(positions) - 300)
        end = min(len(compact), max(positions) + 900)
        return compact[start:end][: cls._DIAGNOSTIC_LIMIT]

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
