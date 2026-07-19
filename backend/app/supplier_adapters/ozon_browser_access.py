from __future__ import annotations

from .errors import AdapterBlockedError
from .ozon_browser import OzonBrowserAdapter
from .playwright_pool import BrowserPageResult


class OzonBrowserAccessAdapter(OzonBrowserAdapter):
    """Ozon browser adapter with explicit anti-bot access classification.

    The base parser owns offer extraction. This concrete access wrapper prevents
    Ozon challenge/offline shells from being misreported as parser failures, so
    SourceHealth and the circuit breaker receive the correct blocked outcome.
    """

    code = "ozon-browser-v5"

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
