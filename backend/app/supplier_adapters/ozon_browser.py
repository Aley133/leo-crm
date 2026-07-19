from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from .base import AccessStrategy, AdapterRequest, NormalizedOffer
from .errors import (
    AdapterBlockedError,
    AdapterCaptchaError,
    AdapterNetworkError,
    AdapterParseError,
    AdapterTimeoutError,
)
from .playwright_pool import (
    PlaywrightBrowserPool,
    PlaywrightNavigationTimeout,
    PlaywrightPoolError,
)

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_OZON_ROOT_DOMAINS = ("ozon.ru", "ozon.kz")
_CAPTCHA_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "проверка безопасности",
)
_BLOCK_MARKERS = (
    "access denied",
    "доступ ограничен",
    "request blocked",
    "temporarily blocked",
)


class OzonBrowserAdapter:
    code = "ozon-browser-v2"
    access_strategy = AccessStrategy.BROWSER

    def __init__(
        self,
        pool: PlaywrightBrowserPool | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._pool = pool or PlaywrightBrowserPool()
        self._timeout_seconds = timeout_seconds

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        try:
            response = await self._pool.fetch_html(
                request.url,
                timeout_seconds=self._timeout_seconds,
            )
        except PlaywrightNavigationTimeout as exc:
            raise AdapterTimeoutError(str(exc)) from exc
        except PlaywrightPoolError as exc:
            raise AdapterNetworkError(str(exc)) from exc

        self._classify_page(response.content)
        offer = self._extract_structured_offer(response.content)
        if offer is None:
            raise AdapterParseError(
                "Ozon browser page did not contain reliable structured offer data"
            )

        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=offer["price"],
            old_price=None,
            available=offer["available"],
            stock=None,
            delivery_days=None,
            seller=offer["seller"],
            adapter_schema_version="ozon-browser-jsonld-v2",
            observed_at=datetime.now(UTC),
            raw_metadata={
                "source": "browser_json_ld",
                "currency": offer["currency"],
                "response_url": response.final_url,
                "duration_ms": response.duration_ms,
                "context_isolation": "fresh_per_check",
            },
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        supported = any(host == root or host.endswith(f".{root}") for root in _OZON_ROOT_DOMAINS)
        if parsed.scheme not in {"http", "https"} or not supported:
            raise ValueError("Ozon browser adapter accepts only ozon.ru or ozon.kz URLs")

    @staticmethod
    def _classify_page(content: str) -> None:
        body = content.casefold()[:250_000]
        if any(marker in body for marker in _CAPTCHA_MARKERS):
            raise AdapterCaptchaError("Ozon returned a captcha page")
        if any(marker in body for marker in _BLOCK_MARKERS):
            raise AdapterBlockedError("Ozon blocked browser access")

    @classmethod
    def _extract_structured_offer(cls, content: str) -> dict[str, Any] | None:
        for raw in _JSON_LD_RE.findall(content):
            try:
                payload = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue
            for product in cls._product_nodes(payload):
                offer = cls._extract_offer(product)
                if offer is not None:
                    return offer
        return None

    @classmethod
    def _product_nodes(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            result: list[dict[str, Any]] = []
            for item in payload:
                result.extend(cls._product_nodes(item))
            return result
        if not isinstance(payload, dict):
            return []

        result: list[dict[str, Any]] = []
        raw_type = payload.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(item).casefold() == "product" for item in types if item is not None):
            result.append(payload)
        if "@graph" in payload:
            result.extend(cls._product_nodes(payload["@graph"]))
        return result

    @staticmethod
    def _extract_offer(product: dict[str, Any]) -> dict[str, Any] | None:
        offers = product.get("offers")
        if isinstance(offers, list):
            offers = next((item for item in offers if isinstance(item, dict)), None)
        if not isinstance(offers, dict):
            return None

        raw_price = offers.get("price") or offers.get("lowPrice")
        try:
            price = Decimal(str(raw_price).replace(" ", "").replace(",", "."))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if price < 0:
            return None

        availability = str(offers.get("availability") or "").casefold()
        if "instock" in availability or "in_stock" in availability:
            available: bool | None = True
        elif any(marker in availability for marker in ("outofstock", "out_of_stock", "soldout")):
            available = False
        else:
            available = None

        seller_value = offers.get("seller")
        seller: str | None = None
        if isinstance(seller_value, dict):
            seller = str(seller_value.get("name") or "").strip() or None
        elif seller_value:
            seller = str(seller_value).strip() or None

        return {
            "price": price,
            "available": available,
            "seller": seller,
            "currency": str(offers.get("priceCurrency") or "RUB").upper(),
        }
