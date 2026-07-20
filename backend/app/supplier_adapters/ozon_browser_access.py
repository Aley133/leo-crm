from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .base import AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .ozon_delivery import OzonDeliveryExtractor
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with anti-bot and scoped delivery semantics."""

    code = "ozon-browser-v12"
    _DIAGNOSTIC_LIMIT = 4000

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        currency = str(offer.raw_metadata.get("currency") or "").strip().upper() or None
        base_delivery_days = offer.delivery_days
        metadata = dict(offer.raw_metadata)
        metadata["base_delivery_days"] = base_delivery_days
        offer = replace(offer, currency=currency, raw_metadata=metadata)

        # Structured Ozon payloads can contain unrelated timing values. The
        # current product's visible add-to-cart promise is authoritative when
        # the managed browser can read it.
        try:
            response, delivery_candidates = await self._fetch_delivery_page(request.url)
        except Exception as exc:
            metadata = dict(offer.raw_metadata)
            metadata["delivery_diagnostic_status"] = "delivery_page_unavailable"
            metadata["delivery_diagnostic_error"] = type(exc).__name__
            return replace(
                offer,
                adapter_schema_version=self.code,
                raw_metadata=metadata,
            )

        self._classify_page(response)
        raw_text = str(response.body_text or "")
        semantic_delivery_days = OzonDeliveryExtractor.from_candidates(
            delivery_candidates,
            fallback_text=raw_text,
        )

        metadata = dict(offer.raw_metadata)
        metadata["semantic_delivery_days"] = semantic_delivery_days
        metadata["delivery_response_url"] = response.final_url
        metadata["delivery_input_length"] = len(raw_text)
        metadata["delivery_candidates"] = delivery_candidates[:10]
        metadata["delivery_context"] = self._delivery_context(
            "\n".join(delivery_candidates) or raw_text
        )
        metadata["delivery_diagnostic_status"] = (
            "semantic_match" if semantic_delivery_days is not None else "semantic_not_found"
        )

        if semantic_delivery_days is not None:
            metadata["delivery_source"] = "ozon_current_product_dom"
            return replace(
                offer,
                delivery_days=semantic_delivery_days,
                adapter_schema_version=self.code,
                raw_metadata=metadata,
            )

        metadata["delivery_source"] = (
            "structured_fallback" if base_delivery_days is not None else "not_found"
        )
        return replace(
            offer,
            adapter_schema_version=self.code,
            raw_metadata=metadata,
        )

    async def _fetch_delivery_page(
        self,
        url: str,
    ) -> tuple[BrowserPageResult, list[str]]:
        async with self._pool.isolated_page() as page:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self._timeout_seconds * 1000),
            )

            # Ozon renders the current-product CTA asynchronously. Waiting for
            # this exact widget prevents recommendation-card dates from becoming
            # the product's delivery promise.
            try:
                await page.wait_for_function(
                    r"""() => Array.from(
                        document.querySelectorAll('[data-widget="webAddToCart"]')
                    ).some((node) => /доставим|доставят|сегодня|завтра|послезавтра|\d{1,2}\s+[а-я]+/i.test(node.innerText || ''))""",
                    timeout=min(15000, int(self._timeout_seconds * 1000)),
                )
            except Exception:
                # Keep the observation successful when price/availability were
                # parsed but delivery is temporarily absent; diagnostics below
                # will show an empty candidate list.
                pass

            # A small viewport movement is enough for Ozon's right-hand PDP
            # widgets, without scrolling into recommendation carousels.
            await page.mouse.wheel(0, 350)
            await page.wait_for_timeout(600)
            await page.mouse.wheel(0, -350)
            await page.wait_for_timeout(600)

            delivery_candidates_raw: Any = await page.evaluate(
                r"""() => {
                    const clean = (value) => String(value || '')
                        .replace(/\u00a0/g, ' ')
                        .replace(/[ \t\r\f\v]+/g, ' ')
                        .replace(/\n+/g, '\n')
                        .trim();
                    const visible = (node) => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            rect.width > 0 && rect.height > 0;
                    };
                    const result = [];
                    const push = (value) => {
                        const text = clean(value);
                        if (text && !result.includes(text)) result.push(text);
                    };

                    // Primary source: only the main PDP add-to-cart widget uses
                    // this data-widget. Recommendation cards use different markup.
                    for (const node of document.querySelectorAll('[data-widget="webAddToCart"]')) {
                        if (visible(node)) push(node.innerText);
                    }

                    // Secondary source: the delivery block of the same PDP. It
                    // can expose an explicit date even when the CTA text is short.
                    for (const heading of document.querySelectorAll('h2')) {
                        if (!visible(heading)) continue;
                        if (!/доставка\s+и\s+возврат/i.test(heading.innerText || '')) continue;
                        const container = heading.closest('[data-widget="webPdpGrid"]') ||
                            heading.parentElement?.parentElement || heading.parentElement;
                        if (container && visible(container)) push(container.innerText);
                    }

                    return result.slice(0, 12);
                }"""
            )
            delivery_candidates = [
                str(value).strip()
                for value in (delivery_candidates_raw or [])
                if str(value or "").strip()
            ]

            body_text = await page.locator("body").inner_text(timeout=10000)
            title = await self._pool._safe_title(page)
            content = await page.content()
            return (
                BrowserPageResult(
                    final_url=page.url,
                    content=content,
                    duration_ms=0,
                    title=title,
                    body_text=str(body_text),
                ),
                delivery_candidates,
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
                "в корзину",
                "пункт выдачи",
                "пункт ozon",
                "постамат",
                "самовывоз",
            )
            if compact.casefold().find(marker) >= 0
        )
        if not markers:
            return compact[: cls._DIAGNOSTIC_LIMIT]
        positions = [compact.casefold().find(marker) for marker in markers]
        start = max(0, min(positions) - 200)
        end = min(len(compact), max(positions) + 700)
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
