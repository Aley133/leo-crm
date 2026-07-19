from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
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
    BrowserPageResult,
    PlaywrightBrowserPool,
    PlaywrightNavigationTimeout,
    PlaywrightPoolError,
)

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_META_PRICE_RE = re.compile(
    r'<meta[^>]+(?:itemprop|property)=["\'](?:price|product:price:amount)["\'][^>]+content=["\']([^"\']+)',
    re.IGNORECASE,
)
_META_CURRENCY_RE = re.compile(
    r'<meta[^>]+(?:itemprop|property)=["\'](?:priceCurrency|product:price:currency)["\'][^>]+content=["\']([^"\']+)',
    re.IGNORECASE,
)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)
_META_RE = re.compile(r"<meta\b", re.IGNORECASE)
_OZON_ROOT_DOMAINS = ("ozon.ru", "ozon.kz")
_CAPTCHA_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "проверка безопасности",
    "verify you are human",
)
_BLOCK_MARKERS = (
    "access denied",
    "доступ ограничен",
    "request blocked",
    "temporarily blocked",
    "forbidden",
)
_CHALLENGE_MARKERS = (
    "challenge",
    "antibot",
    "cloudflare",
    "robot check",
    "проверяем ваш браузер",
)
_PRICE_KEYS = ("price", "finalPrice", "salePrice", "currentPrice", "cardPrice")
_OLD_PRICE_KEYS = ("oldPrice", "originalPrice", "basePrice")


class OzonBrowserAdapter:
    code = "ozon-browser-v4"
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

        self._classify_page(response)
        extracted = self._extract_structured_offer(response.content)
        if extracted is None:
            raise AdapterParseError(
                "Ozon browser page did not contain reliable structured offer data; "
                + self._diagnostic_summary(response)
            )
        offer, source = extracted

        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=offer["price"],
            old_price=offer.get("old_price"),
            available=offer["available"],
            stock=None,
            delivery_days=None,
            seller=offer["seller"],
            adapter_schema_version="ozon-browser-structured-v4",
            observed_at=datetime.now(UTC),
            raw_metadata={
                "source": source,
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
    def _page_text(response: BrowserPageResult) -> str:
        return f"{response.title}\n{response.body_text}\n{response.content[:250_000]}".casefold()

    @classmethod
    def _classify_page(cls, response: BrowserPageResult) -> None:
        body = cls._page_text(response)
        if any(marker in body for marker in _CAPTCHA_MARKERS):
            raise AdapterCaptchaError(
                "Ozon returned a captcha page; " + cls._diagnostic_summary(response)
            )
        if any(marker in body for marker in _BLOCK_MARKERS):
            raise AdapterBlockedError(
                "Ozon blocked browser access; " + cls._diagnostic_summary(response)
            )

    @classmethod
    def _extract_structured_offer(
        cls, content: str
    ) -> tuple[dict[str, Any], str] | None:
        for raw in _JSON_LD_RE.findall(content):
            payload = cls._load_json(raw)
            if payload is None:
                continue
            for product in cls._product_nodes(payload):
                offer = cls._extract_schema_offer(product)
                if offer is not None:
                    return offer, "browser_json_ld"

        for raw in _JSON_SCRIPT_RE.findall(content):
            payload = cls._load_json(raw)
            if payload is None:
                continue
            for node in cls._walk_dicts(payload):
                offer = cls._extract_embedded_offer(node)
                if offer is not None:
                    return offer, "browser_embedded_json"

        meta_price = _META_PRICE_RE.search(content)
        if meta_price:
            price = cls._parse_money(meta_price.group(1))
            if price is not None:
                currency_match = _META_CURRENCY_RE.search(content)
                return {
                    "price": price,
                    "old_price": None,
                    "available": None,
                    "seller": None,
                    "currency": (
                        currency_match.group(1).upper() if currency_match else "RUB"
                    ),
                }, "browser_meta"
        return None

    @staticmethod
    def _load_json(raw: str) -> Any | None:
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            return None

    @classmethod
    def _walk_dicts(cls, payload: Any) -> Iterable[dict[str, Any]]:
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from cls._walk_dicts(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from cls._walk_dicts(item)

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

    @classmethod
    def _extract_schema_offer(cls, product: dict[str, Any]) -> dict[str, Any] | None:
        offers = product.get("offers")
        if isinstance(offers, list):
            offers = next((item for item in offers if isinstance(item, dict)), None)
        if not isinstance(offers, dict):
            return None

        price = cls._parse_money(offers.get("price") or offers.get("lowPrice"))
        if price is None:
            return None
        availability = cls._availability(offers.get("availability"))
        seller_value = offers.get("seller")
        seller: str | None = None
        if isinstance(seller_value, dict):
            seller = str(seller_value.get("name") or "").strip() or None
        elif seller_value:
            seller = str(seller_value).strip() or None
        return {
            "price": price,
            "old_price": None,
            "available": availability,
            "seller": seller,
            "currency": str(offers.get("priceCurrency") or "RUB").upper(),
        }

    @classmethod
    def _extract_embedded_offer(cls, node: dict[str, Any]) -> dict[str, Any] | None:
        price = None
        for key in _PRICE_KEYS:
            if key in node:
                price = cls._parse_money(node.get(key))
                if price is not None:
                    break
        if price is None:
            return None

        keys = {str(key).casefold() for key in node}
        commercial_context = any(
            marker in keys
            for marker in {
                "price",
                "finalprice",
                "saleprice",
                "currentprice",
                "cardprice",
                "availability",
                "seller",
                "sellername",
                "currency",
                "currencycode",
                "isavailable",
            }
        )
        if not commercial_context:
            return None

        old_price = None
        for key in _OLD_PRICE_KEYS:
            if key in node:
                old_price = cls._parse_money(node.get(key))
                if old_price is not None:
                    break
        available = cls._availability(
            node.get("availability", node.get("isAvailable", node.get("available")))
        )
        seller = node.get("sellerName") or node.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("name")
        seller_text = str(seller).strip() if seller not in (None, "") else None
        currency = node.get("currencyCode") or node.get("currency") or "RUB"
        return {
            "price": price,
            "old_price": old_price,
            "available": available,
            "seller": seller_text,
            "currency": str(currency).upper(),
        }

    @staticmethod
    def _parse_money(value: Any) -> Decimal | None:
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        if value is None or isinstance(value, bool):
            return None
        normalized = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
        if not normalized:
            return None
        try:
            price = Decimal(normalized)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price

    @staticmethod
    def _availability(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        text = str(value or "").casefold()
        if any(marker in text for marker in ("instock", "in_stock", "available", "true")):
            return True
        if any(marker in text for marker in ("outofstock", "out_of_stock", "soldout", "false")):
            return False
        return None

    @staticmethod
    def _clean_diagnostic_text(value: str, *, limit: int) -> str:
        compact = " ".join(value.split())
        compact = re.sub(r"[\x00-\x1f\x7f]", "", compact)
        return compact[:limit] or "-"

    @classmethod
    def _diagnostic_summary(cls, response: BrowserPageResult) -> str:
        content = response.content
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        canonical = _CANONICAL_RE.search(content)
        lowered = cls._page_text(response)
        challenge_markers = sorted(
            marker
            for marker in (*_CAPTCHA_MARKERS, *_BLOCK_MARKERS, *_CHALLENGE_MARKERS)
            if marker in lowered
        )
        return (
            f"final_url={response.final_url}; title={cls._clean_diagnostic_text(response.title, limit=160)}; "
            f"body={cls._clean_diagnostic_text(response.body_text, limit=320)}; "
            f"html_bytes={len(content.encode('utf-8'))}; scripts={len(_SCRIPT_RE.findall(content))}; "
            f"meta={len(_META_RE.findall(content))}; json_ld_scripts={len(_JSON_LD_RE.findall(content))}; "
            f"json_scripts={len(_JSON_SCRIPT_RE.findall(content))}; "
            f"canonical={cls._clean_diagnostic_text(canonical.group(1) if canonical else '', limit=200)}; "
            f"challenge={','.join(challenge_markers) or '-'}; sha256={digest}"
        )