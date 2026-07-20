from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .base import AccessStrategy, AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError, AdapterCaptchaError, AdapterNetworkError, AdapterParseError, AdapterTimeoutError
from .playwright_pool import PlaywrightBrowserPool, PlaywrightNavigationTimeout, PlaywrightPoolError

_JSON_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_JSON_SCRIPT_RE = re.compile(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', re.I | re.S)
_META_PRICE_RE = re.compile(r'<meta[^>]+(?:itemprop|property)=["\'](?:price|product:price:amount)["\'][^>]+content=["\']([^"\']+)', re.I)
_META_CURRENCY_RE = re.compile(r'<meta[^>]+(?:itemprop|property)=["\'](?:priceCurrency|product:price:currency)["\'][^>]+content=["\']([^"\']+)', re.I)
_WB_ROOT_DOMAINS = ("wildberries.ru", "wb.ru")
_WB_DESTINATION = "123585444"
_WB_CARD_ENDPOINTS = (
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/v1/detail",
)
_WB_SEARCH_ENDPOINT = "https://search.wb.ru/exactmatch/ru/common/v18/search"


class WildberriesBrowserAccessAdapter:
    code = "wildberries-api-browser-v2"
    access_strategy = AccessStrategy.BROWSER

    def __init__(self, pool: PlaywrightBrowserPool | None = None, *, timeout_seconds: float = 30.0) -> None:
        self._pool = pool or PlaywrightBrowserPool()
        self._timeout_seconds = timeout_seconds

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        nm_id = self._extract_nm_id(request.url)
        api_errors: list[str] = []

        if nm_id is not None:
            for endpoint in _WB_CARD_ENDPOINTS:
                try:
                    payload = await asyncio.to_thread(self._fetch_card_payload, endpoint, nm_id)
                    product = self._find_product(payload, nm_id)
                    if product is not None:
                        offer = self._offer_from_node(product)
                        if offer is not None:
                            return self._normalized_offer(
                                request=request,
                                offer=offer,
                                source="wb_card_api",
                                metadata={"endpoint": endpoint, "nm_id": nm_id},
                            )
                    api_errors.append(f"{endpoint}: product not found")
                except Exception as exc:
                    api_errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")

            try:
                payload = await asyncio.to_thread(self._fetch_search_payload, nm_id)
                product = self._find_product(payload, nm_id)
                if product is not None:
                    offer = self._offer_from_node(product)
                    if offer is not None:
                        return self._normalized_offer(
                            request=request,
                            offer=offer,
                            source="wb_search_api",
                            metadata={"endpoint": _WB_SEARCH_ENDPOINT, "nm_id": nm_id},
                        )
                api_errors.append("search API: product not found")
            except Exception as exc:
                api_errors.append(f"search API: {type(exc).__name__}: {exc}")

        try:
            response = await self._pool.fetch_html(request.url, timeout_seconds=self._timeout_seconds)
        except PlaywrightNavigationTimeout as exc:
            raise AdapterTimeoutError(str(exc)) from exc
        except PlaywrightPoolError as exc:
            raise AdapterNetworkError(str(exc)) from exc

        page_text = f"{response.title}\n{response.body_text}\n{response.content[:250_000]}".casefold()
        if any(marker in page_text for marker in ("captcha", "подтвердите, что вы не робот", "verify you are human")):
            raise AdapterCaptchaError("Wildberries returned a captcha page")
        if any(marker in page_text for marker in ("access denied", "доступ ограничен", "request blocked", "forbidden")):
            raise AdapterBlockedError("Wildberries blocked browser access")

        offer = self._extract_offer(response.content)
        if offer is None:
            diagnostics = "; ".join(api_errors[-4:]) or "API diagnostics unavailable"
            raise AdapterParseError(
                "Wildberries did not return reliable offer data via API or browser; " + diagnostics
            )

        return self._normalized_offer(
            request=request,
            offer=offer,
            source="wb_browser_fallback",
            metadata={"response_url": response.final_url, "duration_ms": response.duration_ms, "nm_id": nm_id},
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
            adapter_schema_version="wildberries-api-browser-v2",
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
            "Accept": "application/json",
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
                "spp": "30",
                "nm": str(nm_id),
            }
        )
        return self._get_json(f"{endpoint}?{query}")

    def _fetch_search_payload(self, nm_id: int) -> dict[str, Any]:
        query = urlencode(
            {
                "ab_testing": "false",
                "appType": "1",
                "curr": "kzt",
                "dest": _WB_DESTINATION,
                "query": str(nm_id),
                "resultset": "catalog",
                "sort": "popular",
                "spp": "30",
            }
        )
        return self._get_json(f"{_WB_SEARCH_ENDPOINT}?{query}")

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
            raise AdapterNetworkError(f"WB API unavailable: {exc}") from exc
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
        exact: dict[str, Any] | None = None
        fallback: dict[str, Any] | None = None
        for node in cls._walk_dicts(payload):
            if not cls._looks_like_product(node):
                continue
            fallback = fallback or node
            raw_id = node.get("id", node.get("nmId", node.get("nmID")))
            try:
                if raw_id is not None and int(raw_id) == nm_id:
                    exact = node
                    break
            except (TypeError, ValueError):
                continue
        return exact or fallback

    @staticmethod
    def _looks_like_product(node: dict[str, Any]) -> bool:
        keys = set(node)
        has_price = bool(keys & {"salePriceU", "priceU", "salePrice", "finalPrice", "price", "currentPrice"})
        has_identity = bool(keys & {"id", "nmId", "nmID", "name", "brand"})
        return has_price and has_identity

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
                return {"price": price, "currency": currency.group(1).upper() if currency else "RUB", "available": None}
        return None

    @classmethod
    def _offer_from_node(cls, node: dict[str, Any]) -> dict[str, Any] | None:
        price = None
        for key in ("salePriceU", "priceU", "salePrice", "finalPrice", "price", "currentPrice"):
            if key not in node:
                continue
            price = cls._money(node.get(key), divide_100=key.endswith("U"))
            if price is not None:
                break
        if price is None:
            return None

        old_price = None
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

        seller = node.get("supplierName") or node.get("sellerName") or node.get("seller") or node.get("supplier")
        if isinstance(seller, dict):
            seller = seller.get("name")

        delivery_days = None
        for key in ("deliveryDays", "delivery_days", "maxDeliveryDays", "minDeliveryDays", "time1", "time2"):
            delivery_days = cls._integer(node.get(key))
            if delivery_days is not None:
                break

        currency = str(node.get("currencyCode") or node.get("currency") or "KZT").upper()
        return {
            "price": price,
            "old_price": old_price if old_price != price else None,
            "currency": currency,
            "available": available,
            "stock": stock,
            "delivery_days": delivery_days,
            "seller": str(seller).strip() if seller not in (None, "") else None,
        }

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
