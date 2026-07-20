from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlparse

from .base import AccessStrategy, AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError, AdapterCaptchaError, AdapterNetworkError, AdapterParseError, AdapterTimeoutError
from .playwright_pool import PlaywrightBrowserPool, PlaywrightNavigationTimeout, PlaywrightPoolError

_JSON_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
_JSON_SCRIPT_RE = re.compile(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', re.I | re.S)
_META_PRICE_RE = re.compile(r'<meta[^>]+(?:itemprop|property)=["\'](?:price|product:price:amount)["\'][^>]+content=["\']([^"\']+)', re.I)
_META_CURRENCY_RE = re.compile(r'<meta[^>]+(?:itemprop|property)=["\'](?:priceCurrency|product:price:currency)["\'][^>]+content=["\']([^"\']+)', re.I)
_WB_ROOT_DOMAINS = ("wildberries.ru", "wb.ru")


class WildberriesBrowserAccessAdapter:
    code = "wildberries-browser-v1"
    access_strategy = AccessStrategy.BROWSER

    def __init__(self, pool: PlaywrightBrowserPool | None = None, *, timeout_seconds: float = 30.0) -> None:
        self._pool = pool or PlaywrightBrowserPool()
        self._timeout_seconds = timeout_seconds

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
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
            raise AdapterParseError("Wildberries page did not contain reliable structured offer data")

        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=offer["price"],
            old_price=offer.get("old_price"),
            currency=offer.get("currency") or "RUB",
            available=offer.get("available"),
            stock=offer.get("stock"),
            delivery_days=offer.get("delivery_days"),
            seller=offer.get("seller"),
            adapter_schema_version="wildberries-browser-structured-v1",
            observed_at=datetime.now(UTC),
            raw_metadata={"response_url": response.final_url, "duration_ms": response.duration_ms},
        )

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        supported = any(host == root or host.endswith(f".{root}") for root in _WB_ROOT_DOMAINS)
        if parsed.scheme not in {"http", "https"} or not supported:
            raise ValueError("Wildberries browser adapter accepts only wildberries.ru or wb.ru URLs")

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

        stock = cls._integer(node.get("totalQuantity", node.get("quantity", node.get("stock"))))
        available_raw = node.get("available", node.get("isAvailable", node.get("availability")))
        available = cls._availability(available_raw)
        if available is None and stock is not None:
            available = stock > 0

        seller = node.get("supplierName") or node.get("sellerName") or node.get("seller") or node.get("supplier")
        if isinstance(seller, dict):
            seller = seller.get("name")

        delivery_days = None
        for key in ("deliveryDays", "delivery_days", "maxDeliveryDays", "minDeliveryDays"):
            delivery_days = cls._integer(node.get(key))
            if delivery_days is not None:
                break

        currency = str(node.get("currencyCode") or node.get("currency") or "RUB").upper()
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
