from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .base import AccessStrategy, AdapterRequest, NormalizedOffer
from .errors import (
    AdapterBlockedError,
    AdapterCaptchaError,
    AdapterNetworkError,
    AdapterParseError,
    AdapterTimeoutError,
)
from .playwright_pool import PlaywrightBrowserPool, PlaywrightNavigationTimeout, PlaywrightPoolError

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_JSON_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_META_PRICE_RE = re.compile(
    r'<meta[^>]+(?:itemprop|property)=["\'](?:price|product:price:amount)["\'][^>]+content=["\']([^"\']+)',
    re.I,
)
_META_CURRENCY_RE = re.compile(
    r'<meta[^>]+(?:itemprop|property)=["\'](?:priceCurrency|product:price:currency)["\'][^>]+content=["\']([^"\']+)',
    re.I,
)
_WB_ROOT_DOMAINS = ("wildberries.ru", "wb.ru")
_WB_DESTINATION = "123585444"
_WB_CARD_ENDPOINTS = (
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/v1/detail",
)
_WB_SEARCH_ENDPOINTS = (
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
)


class WildberriesBrowserAccessAdapter:
    code = "wildberries-api-browser-v3"
    access_strategy = AccessStrategy.BROWSER

    def __init__(
        self,
        pool: PlaywrightBrowserPool | None = None,
        *,
        timeout_seconds: float = 12.0,
        browser_fallback: bool | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._pool = pool or PlaywrightBrowserPool()
        self._timeout_seconds = timeout_seconds
        self._browser_fallback = (
            browser_fallback
            if browser_fallback is not None
            else (os.getenv("WB_BROWSER_FALLBACK") or "").strip().casefold() in {"1", "true", "yes"}
        )

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        nm_id = self._extract_nm_id(request.url)
        if nm_id is None:
            raise AdapterParseError("Wildberries nmId was not found in supplier URL")

        api_errors: list[str] = []
        for endpoint in _WB_CARD_ENDPOINTS:
            offer = await self._try_api_endpoint(
                request=request,
                nm_id=nm_id,
                endpoint=endpoint,
                source="wb_card_api",
                loader=lambda endpoint=endpoint: self._fetch_card_payload(endpoint, nm_id),
                errors=api_errors,
            )
            if offer is not None:
                return offer

        for endpoint in _WB_SEARCH_ENDPOINTS:
            offer = await self._try_api_endpoint(
                request=request,
                nm_id=nm_id,
                endpoint=endpoint,
                source="wb_search_api",
                loader=lambda endpoint=endpoint: self._fetch_search_payload(endpoint, nm_id),
                errors=api_errors,
            )
            if offer is not None:
                return offer

        if self._browser_fallback:
            return await self._fetch_browser_fallback(request, nm_id=nm_id, api_errors=api_errors)

        diagnostics = "; ".join(api_errors) or "no WB API diagnostics"
        raise AdapterParseError(
            f"Wildberries APIs returned no usable offer for nmId={nm_id}; {diagnostics}"
        )

    async def _try_api_endpoint(
        self,
        *,
        request: AdapterRequest,
        nm_id: int,
        endpoint: str,
        source: str,
        loader,
        errors: list[str],
    ) -> NormalizedOffer | None:
        try:
            payload = await asyncio.to_thread(loader)
            product = self._find_product(payload, nm_id)
            if product is None:
                errors.append(f"{endpoint}: product not found")
                return None
            offer = self._offer_from_node(product)
            if offer is None:
                errors.append(f"{endpoint}: product found but price unavailable")
                return None
            return self._normalized_offer(
                request=request,
                offer=offer,
                source=source,
                metadata={"endpoint": endpoint, "nm_id": nm_id},
            )
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
            return None

    async def _fetch_browser_fallback(
        self,
        request: AdapterRequest,
        *,
        nm_id: int,
        api_errors: list[str],
    ) -> NormalizedOffer:
        try:
            response = await self._pool.fetch_html(
                request.url,
                timeout_seconds=self._timeout_seconds,
            )
        except PlaywrightNavigationTimeout as exc:
            diagnostics = "; ".join(api_errors)
            raise AdapterTimeoutError(f"WB browser fallback timed out; {diagnostics}") from exc
        except PlaywrightPoolError as exc:
            diagnostics = "; ".join(api_errors)
            raise AdapterNetworkError(f"WB browser fallback failed; {diagnostics}") from exc

        page_text = f"{response.title}\n{response.body_text}\n{response.content[:250_000]}".casefold()
        if any(
            marker in page_text
            for marker in ("captcha", "подтвердите, что вы не робот", "verify you are human")
        ):
            raise AdapterCaptchaError("Wildberries returned a captcha page")
        if any(
            marker in page_text
            for marker in ("access denied", "доступ ограничен", "request blocked", "forbidden")
        ):
            raise AdapterBlockedError("Wildberries blocked browser access")

        offer = self._extract_offer(response.content)
        if offer is None:
            diagnostics = "; ".join(api_errors)
            raise AdapterParseError(
                f"Wildberries browser fallback returned no offer for nmId={nm_id}; {diagnostics}"
            )
        return self._normalized_offer(
            request=request,
            offer=offer,
            source="wb_browser_fallback",
            metadata={
                "response_url": response.final_url,
                "duration_ms": response.duration_ms,
                "nm_id": nm_id,
            },
        )

    @classmethod
    def _normalized_offer(
        cls,
        *,
        request: AdapterRequest,
        offer: dict[str, Any],
        source: str,
        metadata: dict[str, Any],
    ) -> NormalizedOffer:
        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=offer["price"],
            old_price=offer.get("old_price"),
            currency=offer.get("currency") or "KZT",
            available=offer.get("available"),
            stock=offer.get("stock"),
            delivery_days=offer.get("delivery_days"),
            seller=offer.get("seller"),
            adapter_schema_version="wildberries-api-browser-v3",
            observed_at=datetime.now(UTC),
            raw_metadata={"source": source, **metadata},
        )

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }

    def _fetch_card_payload(self, endpoint: str, nm_id: int) -> dict[str, Any]:
        query = urlencode(
            {
                "appType": "1",
                "curr": "kzt",
                "dest": _WB_DESTINATION,
                "lang": "ru",
                "spp": "30",
                "nm": str(nm_id),
            }
        )
        return self._get_json(f"{endpoint}?{query}")

    def _fetch_search_payload(self, endpoint: str, nm_id: int) -> dict[str, Any]:
        query = urlencode(
            {
                "ab_testing": "false",
                "appType": "1",
                "curr": "kzt",
                "dest": _WB_DESTINATION,
                "lang": "ru",
                "query": str(nm_id),
                "resultset": "catalog",
                "sort": "popular",
                "spp": "30",
            }
        )
        return self._get_json(f"{endpoint}?{query}")

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise AdapterNetworkError(f"WB API returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise AdapterNetworkError(f"WB API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise AdapterNetworkError(f"WB API unavailable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AdapterTimeoutError("WB API request timed out") from exc
        except json.JSONDecodeError as exc:
            raise AdapterParseError("WB API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterParseError("WB API returned an unexpected payload")
        return payload

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        supported = any(host == root or host.endswith(f".{root}") for root in _WB_ROOT_DOMAINS)
        if parsed.scheme not in {"http", "https"} or not supported:
            raise ValueError("Wildberries adapter accepts only wildberries.ru or wb.ru URLs")

    @staticmethod
    def _extract_nm_id(url: str) -> int | None:
        parsed = urlparse(url)
        candidates = re.findall(r"(?:catalog/|product/|nm=)(\d{5,})", f"{parsed.path}?{parsed.query}")
        if not candidates:
            candidates = re.findall(r"\b(\d{5,})\b", parsed.path)
        return int(candidates[0]) if candidates else None

    @classmethod
    def _find_product(cls, payload: Any, nm_id: int) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for node in cls._walk_dicts(payload):
            if not cls._looks_like_product(node):
                continue
            fallback = fallback or node
            raw_id = node.get("id", node.get("nmId", node.get("nmID")))
            try:
                if raw_id is not None and int(raw_id) == nm_id:
                    return node
            except (TypeError, ValueError):
                continue
        return fallback

    @classmethod
    def _looks_like_product(cls, node: dict[str, Any]) -> bool:
        keys = set(node)
        has_identity = bool(keys & {"id", "nmId", "nmID", "name", "brand"})
        if not has_identity:
            return False
        if keys & {"salePriceU", "priceU", "salePrice", "finalPrice", "currentPrice"}:
            return True
        direct_price = node.get("price")
        if direct_price not in (None, ""):
            return True
        sizes = node.get("sizes")
        return isinstance(sizes, list) and any(
            isinstance(size, dict) and isinstance(size.get("price"), dict)
            for size in sizes
        )

    @classmethod
    def _extract_offer(cls, content: str) -> dict[str, Any] | None:
        for raw in (*_JSON_LD_RE.findall(content), *_JSON_SCRIPT_RE.findall(content)):
            try:
                payload = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue
            for node in cls._walk_dicts(payload):
                offer = cls._offer_from_node(node)
                if offer is not None:
                    return offer
        meta = _META_PRICE_RE.search(content)
        if meta:
            price = cls._money(meta.group(1))
            if price is not None:
                currency = _META_CURRENCY_RE.search(content)
                return {
                    "price": price,
                    "currency": currency.group(1).upper() if currency else "RUB",
                    "available": None,
                }
        return None

    @classmethod
    def _offer_from_node(cls, node: dict[str, Any]) -> dict[str, Any] | None:
        price = None
        old_price = None
        for key in ("salePriceU", "priceU", "salePrice", "finalPrice", "currentPrice"):
            if key not in node:
                continue
            price = cls._money(node.get(key), divide_100=key.endswith("U"))
            if price is not None:
                break

        direct_price = node.get("price")
        if price is None and not isinstance(direct_price, dict):
            price = cls._money(direct_price)

        nested_price, nested_old_price = cls._price_from_sizes(node.get("sizes"))
        price = price or nested_price
        old_price = nested_old_price
        if price is None:
            return None

        if old_price is None:
            for key in ("priceU", "oldPrice", "basicPrice", "originalPrice"):
                if key in node:
                    old_price = cls._money(node.get(key), divide_100=key.endswith("U"))
                    if old_price is not None:
                        break

        stock = cls._stock_from_node(node)
        available_raw = node.get("available", node.get("isAvailable", node.get("availability")))
        available = cls._availability(available_raw)
        if available is None and stock is not None:
            available = stock > 0

        seller = (
            node.get("supplierName")
            or node.get("sellerName")
            or node.get("seller")
            or node.get("supplier")
        )
        if isinstance(seller, dict):
            seller = seller.get("name")

        delivery_days = None
        for key in ("deliveryDays", "delivery_days", "maxDeliveryDays", "minDeliveryDays"):
            delivery_days = cls._integer(node.get(key))
            if delivery_days is not None:
                break

        currency = str(node.get("currencyCode") or node.get("currency") or "KZT").upper()
        return {
            "price": price,
            "old_price": old_price if old_price and old_price != price else None,
            "currency": currency,
            "available": available,
            "stock": stock,
            "delivery_days": delivery_days,
            "seller": str(seller).strip() if seller not in (None, "") else None,
        }

    @classmethod
    def _price_from_sizes(cls, sizes: Any) -> tuple[Decimal | None, Decimal | None]:
        if not isinstance(sizes, list):
            return None, None
        for size in sizes:
            if not isinstance(size, dict):
                continue
            price_data = size.get("price")
            if not isinstance(price_data, dict):
                continue
            final_price = None
            for key in ("product", "total"):
                final_price = cls._money(price_data.get(key), divide_100=True)
                if final_price is not None:
                    break
            basic_price = cls._money(price_data.get("basic"), divide_100=True)
            if final_price is not None:
                return final_price, basic_price
        return None, None

    @classmethod
    def _stock_from_node(cls, node: dict[str, Any]) -> int | None:
        for key in ("totalQuantity", "quantity", "stock", "totalStock"):
            value = cls._integer(node.get(key))
            if value is not None:
                return value
        sizes = node.get("sizes")
        if isinstance(sizes, list):
            total = 0
            found = False
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                stocks = size.get("stocks")
                if not isinstance(stocks, list):
                    continue
                for stock in stocks:
                    if not isinstance(stock, dict):
                        continue
                    qty = cls._integer(stock.get("qty", stock.get("quantity")))
                    if qty is not None:
                        total += qty
                        found = True
            if found:
                return total
        return None

    @classmethod
    def _walk_dicts(cls, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_dicts(child)

    @staticmethod
    def _money(value: Any, *, divide_100: bool = False) -> Decimal | None:
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        if value is None or isinstance(value, bool):
            return None
        normalized = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
        if not normalized:
            return None
        try:
            result = Decimal(normalized)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if divide_100:
            result /= Decimal("100")
        return result if result > 0 else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

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
