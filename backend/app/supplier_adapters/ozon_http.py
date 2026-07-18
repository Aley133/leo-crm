from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import AdapterRequest, NormalizedOffer
from .errors import (
    AdapterAuthRequiredError,
    AdapterBlockedError,
    AdapterCaptchaError,
    AdapterNetworkError,
    AdapterNotFoundError,
    AdapterParseError,
    AdapterRateLimitedError,
    AdapterTimeoutError,
)

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_CAPTCHA_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "проверка безопасности",
    "access denied",
)
_BLOCK_MARKERS = (
    "доступ ограничен",
    "temporarily blocked",
    "request blocked",
)
_OZON_ROOT_DOMAINS = ("ozon.ru", "ozon.kz")


class OzonHttpAdapter:
    """Conservative direct-HTTP adapter for Ozon Russia and Kazakhstan.

    The adapter does not attempt to bypass anti-bot systems. It accepts only
    reliable structured product data and classifies blocked/captcha responses
    explicitly instead of pretending that a product is absent.
    """

    code = "ozon-http-v1"
    access_strategy = "direct_http"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        }

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        try:
            try:
                response = await client.get(request.url, headers=headers)
            except httpx.TimeoutException as exc:
                raise AdapterTimeoutError(str(exc) or "Ozon request timed out") from exc
            except httpx.NetworkError as exc:
                raise AdapterNetworkError(str(exc) or "Ozon network request failed") from exc

            self._raise_for_response(response)
            return self._parse_product_page(request, response)
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        supported_host = any(host == root or host.endswith(f".{root}") for root in _OZON_ROOT_DOMAINS)
        if parsed.scheme not in {"http", "https"} or not supported_host:
            raise ValueError("Ozon adapter accepts only ozon.ru or ozon.kz URLs")

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        status = response.status_code
        body = response.text.casefold()[:250_000]

        if status == 404:
            raise AdapterNotFoundError(http_status=status)
        if status == 429:
            raise AdapterRateLimitedError(http_status=status)
        if status in {401, 407}:
            raise AdapterAuthRequiredError(http_status=status)
        if status in {403, 451}:
            if any(marker in body for marker in _CAPTCHA_MARKERS):
                raise AdapterCaptchaError(http_status=status)
            raise AdapterBlockedError(http_status=status)
        if status >= 500:
            raise AdapterNetworkError(f"Ozon returned HTTP {status}")
        if status >= 400:
            raise AdapterBlockedError(f"Ozon returned HTTP {status}", http_status=status)

        if any(marker in body for marker in _CAPTCHA_MARKERS):
            raise AdapterCaptchaError(http_status=status)
        if any(marker in body for marker in _BLOCK_MARKERS):
            raise AdapterBlockedError(http_status=status)

    def _parse_product_page(self, request: AdapterRequest, response: httpx.Response) -> NormalizedOffer:
        candidates: list[dict[str, Any]] = []
        for raw in _JSON_LD_RE.findall(response.text):
            try:
                payload = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue
            candidates.extend(self._product_nodes(payload))

        for product in candidates:
            offer = self._extract_offer(product)
            if offer is None:
                continue
            return NormalizedOffer(
                supplier_product_id=request.supplier_product_id,
                price=offer["price"],
                old_price=None,
                available=offer["available"],
                stock=None,
                delivery_days=None,
                seller=offer["seller"],
                adapter_schema_version="ozon-jsonld-v1",
                observed_at=datetime.now(UTC),
                raw_metadata={
                    "source": "json_ld",
                    "currency": offer["currency"],
                    "response_url": str(response.url),
                },
            )

        raise AdapterParseError(
            "Ozon response did not contain reliable structured offer data",
            http_status=response.status_code,
        )

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
        type_value = payload.get("@type")
        types = type_value if isinstance(type_value, list) else [type_value]
        if any(str(item).casefold() == "product" for item in types if item is not None):
            result.append(payload)
        graph = payload.get("@graph")
        if graph is not None:
            result.extend(cls._product_nodes(graph))
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
        available: bool | None
        if "instock" in availability or "in_stock" in availability:
            available = True
        elif "outofstock" in availability or "out_of_stock" in availability or "soldout" in availability:
            available = False
        else:
            available = None

        seller_value = offers.get("seller")
        seller: str | None = None
        if isinstance(seller_value, dict):
            candidate = seller_value.get("name")
            seller = str(candidate).strip() if candidate else None
        elif seller_value:
            seller = str(seller_value).strip() or None

        currency = str(offers.get("priceCurrency") or "RUB").upper()
        return {
            "price": price,
            "available": available,
            "seller": seller,
            "currency": currency,
        }
