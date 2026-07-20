from __future__ import annotations

from dataclasses import replace

from .base import AdapterRequest, NormalizedOffer
from .delivery_normalizer import DeliveryNormalizer
from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with anti-bot and delivery normalization."""

    code = "ozon-browser-v8"

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        currency = str(offer.raw_metadata.get("currency") or "").strip().upper() or None
        offer = replace(offer, currency=currency)
        if offer.delivery_days is not None:
            return offer

        # Ozon frequently exposes price in structured JSON but delivery only in
        # visible text. Restrict parsing to delivery-related page context so
        # promotion countdowns such as "10 дней до конца" are ignored.
        response = await self._pool.fetch_html(
            request.url,
            timeout_seconds=self._timeout_seconds,
        )
        self._classify_page(response)
        delivery_days = DeliveryNormalizer.from_context(
            response.body_text,
            markers=(
                "доставим",
                "доставка",
                "получить",
                "получение",
                "пункт ozon",
                "пункт выдачи",
                "курьер",
                "самовывоз",
            ),
            excluded_phrases=(
                "до конца",
                "распродажа",
                "акция",
                "скидка",
                "купон",
                "осталось",
            ),
            window=2,
        )
        if delivery_days is None:
            return offer

        metadata = dict(offer.raw_metadata)
        metadata["delivery_source"] = "visible_delivery_context_normalized"
        metadata["delivery_response_url"] = response.final_url
        return replace(
            offer,
            delivery_days=delivery_days,
            adapter_schema_version=self.code,
            raw_metadata=metadata,
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
