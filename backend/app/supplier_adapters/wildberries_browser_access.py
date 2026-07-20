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
from .errors import AdapterNetworkError, AdapterParseError, AdapterTimeoutError
from .playwright_pool import PlaywrightBrowserPool

_WB_ROOT_DOMAINS = ("wildberries.ru", "wb.ru")
_WB_DESTINATIONS = ("123585444", "-1257786")
_WB_CARD_ENDPOINTS = (
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/detail",
)


class WildberriesBrowserAccessAdapter:
    """Wildberries adapter using the proven TGBAD card API strategy.

    The historical class name is preserved because Browser Agent imports it,
    but normal monitoring is API-only. Product, queue, lease and observation
    boundaries remain outside this adapter.
    """

    code = "wildberries-card-api-v4"
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
        # Kept only for constructor compatibility. WB production monitoring is
        # intentionally API-only to avoid long, unreliable page navigation.
        self._browser_fallback = False

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        nm_id = self._extract_nm_id(request.url)
        if nm_id is None:
            raise AdapterParseError("Wildberries nmId was not found in supplier URL")

        errors: list[str] = []
        for destination in _WB_DESTINATIONS:
            for endpoint in _WB_CARD_ENDPOINTS:
                try:
                    payload = await asyncio.to_thread(
                        self._fetch_card_payload,
                        endpoint,
                        nm_id,
                        destination,
                        request.url,
                    )
                    product = self._find_product(payload, nm_id)
                    if product is None:
                        errors.append(f"{endpoint} dest={destination}: product not found")
                        continue
                    offer = self._offer_from_node(product)
                    if offer is None:
                        errors.append(f"{endpoint} dest={destination}: price unavailable")
                        continue
                    return self._normalized_offer(
                        request=request,
                        offer=offer,
                        endpoint=endpoint,
                        destination=destination,
                        nm_id=nm_id,
                    )
                except Exception as exc:
                    errors.append(
                        f"{endpoint} dest={destination}: {type(exc).__name__}: {exc}"
                    )
                    if "HTTP 429" in str(exc):
                        break

        diagnostics = "; ".join(errors) or "no WB API diagnostics"
        raise AdapterParseError(
            f"Wildberries card APIs returned no usable offer for nmId={nm_id}; {diagnostics}"
        )

    @classmethod
    def _normalized_offer(
        cls,
        *,
        request: AdapterRequest,
        offer: dict[str, Any],
        endpoint: str,
        destination: str,
        nm_id: int,
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
            adapter_schema_version="wildberries-card-api-v4",
            observed_at=datetime.now(UTC),
            raw_metadata={
                "source": "wb_card_api",
                "endpoint": endpoint,
                "destination": destination,
                "nm_id": nm_id,
            },
        )

    @staticmethod
    def _headers(referer_url: str) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Origin": "https://www.wildberries.ru",
            "Referer": referer_url,
        }

    def _fetch_card_payload(
        self,
        endpoint: str,
        nm_id: int,
        destination: str,
        referer_url: str,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "appType": "1",
                "curr": "kzt",
                "dest": destination,
                "spp": "30",
                "ab_testing": "false",
                "lang": "ru",
                "nm": str(nm_id),
            }
        )
        return self._get_json(
            f"{endpoint}?{query}",
            headers=self._headers(referer_url),
        )

    def _get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
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
        candidates = re.findall(
            r"(?:catalog/|product/|nm=)(\d{5,})",
            f"{parsed.path}?{parsed.query}",
        )
        if not candidates:
            candidates = re.findall(r"\b(\d{5,})\b", parsed.path)
        if not candidates:
            return None
        value = int(candidates[0])
        return value if value > 0 else None

    @classmethod
    def _find_product(cls, payload: Any, nm_id: int) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for node in cls._walk_dicts(payload):
            if not cls._looks_like_product(node):
                continue
            fallback = fallback or node
            raw_id = node.get("id", node.get("nmId", node.get("nmID", node.get("nm_id"))))
            try:
                if raw_id is not None and int(raw_id) == nm_id:
                    return node
            except (TypeError, ValueError):
                continue
        return fallback

    @classmethod
    def _looks_like_product(cls, node: dict[str, Any]) -> bool:
        keys = set(node)
        has_identity = bool(keys & {"id", "nmId", "nmID", "nm_id", "name", "brand"})
        if not has_identity:
            return False
        if keys & {
            "salePriceU",
            "priceU",
            "salePrice",
            "price",
            "sale_price",
            "finalPrice",
            "clientPrice",
        }:
            return True
        sizes = node.get("sizes")
        return isinstance(sizes, list) and any(
            isinstance(size, dict)
            and (
                isinstance(size.get("price"), dict)
                or any(key in size for key in ("salePriceU", "priceU", "price"))
            )
            for size in sizes
        )

    @classmethod
    def _offer_from_node(cls, node: dict[str, Any]) -> dict[str, Any] | None:
        price = cls._extract_price(node)
        if price is None:
            return None
        old_price = cls._extract_old_price(node)
        stock = cls._stock_from_node(node)
        available = cls._availability(
            node.get("available", node.get("isAvailable", node.get("availability")))
        )
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

        return {
            "price": price,
            "old_price": old_price if old_price and old_price != price else None,
            "currency": str(node.get("currencyCode") or node.get("currency") or "KZT").upper(),
            "available": available,
            "stock": stock,
            "delivery_days": cls._delivery_days(node),
            "seller": str(seller).strip() if seller not in (None, "") else None,
        }

    @classmethod
    def _extract_price(cls, node: dict[str, Any]) -> Decimal | None:
        for key in (
            "salePriceU",
            "priceU",
            "salePrice",
            "price",
            "sale_price",
            "finalPrice",
            "clientPrice",
        ):
            value = node.get(key)
            if isinstance(value, dict):
                continue
            price = cls._normalize_wb_price(value, minimal_units=key.endswith("U"))
            if price is not None:
                return price

        sizes = node.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                price_info = size.get("price")
                if isinstance(price_info, dict):
                    for key in ("total", "product", "basic", "final", "discounted", "price"):
                        price = cls._normalize_wb_price(price_info.get(key), minimal_units=True)
                        if price is not None:
                            return price
                for key in ("salePriceU", "priceU", "price"):
                    price = cls._normalize_wb_price(
                        size.get(key),
                        minimal_units=key.endswith("U"),
                    )
                    if price is not None:
                        return price
        return None

    @classmethod
    def _extract_old_price(cls, node: dict[str, Any]) -> Decimal | None:
        for key in ("priceU", "oldPrice", "basicPrice", "originalPrice"):
            price = cls._normalize_wb_price(
                node.get(key),
                minimal_units=key.endswith("U"),
            )
            if price is not None:
                return price
        sizes = node.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                price_info = size.get("price")
                if isinstance(price_info, dict):
                    price = cls._normalize_wb_price(price_info.get("basic"), minimal_units=True)
                    if price is not None:
                        return price
        return None

    @classmethod
    def _stock_from_node(cls, node: dict[str, Any]) -> int | None:
        for key in ("totalQuantity", "quantity", "stock", "totalStock"):
            value = cls._integer(node.get(key))
            if value is not None:
                return value
        sizes = node.get("sizes")
        if not isinstance(sizes, list):
            return None
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
        return total if found else None

    @classmethod
    def _delivery_days(cls, node: dict[str, Any]) -> int | None:
        values: list[int] = []
        for key in ("deliveryTime", "delivery_time", "time1", "time2", "time"):
            value = cls._integer(node.get(key))
            if value is not None:
                values.append(value)
        nested = node.get("logistics") or node.get("delivery")
        if isinstance(nested, dict):
            for key in ("deliveryTime", "delivery_time", "time1", "time2", "time", "days"):
                value = cls._integer(nested.get(key))
                if value is not None:
                    values.append(value)
        values = [value for value in values if 0 <= value <= 720]
        if not values:
            return None
        value = min(values)
        if value > 30:
            return max(0, min(30, (value + 23) // 24))
        return max(0, min(30, value))

    @staticmethod
    def _normalize_wb_price(value: Any, *, minimal_units: bool) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        normalized = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
        if not normalized:
            return None
        try:
            result = Decimal(normalized)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if minimal_units or result >= Decimal("100000"):
            result /= Decimal("100")
        return result if result >= Decimal("100") else None

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
