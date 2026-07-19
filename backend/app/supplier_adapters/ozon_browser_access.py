from __future__ import annotations

from dataclasses import replace

from .base import AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with explicit anti-bot access classification."""

    code = "ozon-browser-v6"

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        currency = str(offer.raw_metadata.get("currency") or "").strip().upper() or None
        return replace(offer, currency=currency)

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
