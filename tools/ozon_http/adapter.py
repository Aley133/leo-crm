from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from backend.app.supplier_adapters.base import AccessStrategy, AdapterRequest, NormalizedOffer
from backend.app.supplier_adapters.errors import (
    AdapterAuthRequiredError,
    AdapterBlockedError,
    AdapterNetworkError,
    AdapterNotFoundError,
    AdapterParseError,
    AdapterRateLimitedError,
    AdapterTimeoutError,
)

from .resolver import OzonSessionResolver, OzonSessionUnavailableError
from .session_client import OzonSessionHttpClient


class OzonSessionHttpAdapter:
    """Fast Ozon adapter backed by the lab's accepted HTTP session."""

    code = "ozon-http-session-v1"
    access_strategy = AccessStrategy.DIRECT_HTTP

    def __init__(self, resolver: OzonSessionResolver | None = None) -> None:
        self.resolver = resolver or OzonSessionResolver()
        # The storefront session is fast, but bursts from three CRM workers
        # were producing gateway/network noise. Two in-flight reads still scan
        # the whole catalog quickly without hammering one imported session.
        self._request_gate = asyncio.Semaphore(2)

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        try:
            async with self._request_gate:
                result = await asyncio.to_thread(self._fetch_sync, request.url, request.external_id)
        except OzonSessionUnavailableError as exc:
            raise AdapterAuthRequiredError(str(exc)) from exc
        except TimeoutError as exc:
            raise AdapterTimeoutError(str(exc)) from exc
        except AdapterParseError:
            raise
        except Exception as exc:
            name = type(exc).__name__.casefold()
            if "timeout" in name:
                raise AdapterTimeoutError(str(exc)) from exc
            if "connect" in name or "network" in name or "curl" in name:
                raise AdapterNetworkError(str(exc)) from exc
            raise AdapterParseError(f"Ozon HTTP response could not be normalized: {exc}") from exc

        attempt = result.get("attempt") or {}
        status = int(attempt.get("status_code") or 0)
        if status == 404:
            raise AdapterNotFoundError()
        if status == 429:
            raise AdapterRateLimitedError()
        if status in {401, 407}:
            self.resolver.invalidate()
            raise AdapterAuthRequiredError(http_status=status)
        if attempt.get("blocked") or status in {403, 451}:
            raise AdapterBlockedError(http_status=status or None)
        if status != 200 or not result.get("ok"):
            raise AdapterNetworkError(f"Ozon HTTP request failed with status {status or 'unknown'}")

        offer: dict[str, Any] = result.get("cheaper_offer") or {}
        price = result.get("cheaper_price_kzt")
        if not isinstance(price, int) or price <= 0:
            authoritative_empty = (
                "other_offer_count" in result
                and "other_offers" in result
                and int(result.get("other_offer_count") or 0) == 0
                and result.get("other_offers") == []
            )
            if authoritative_empty:
                return NormalizedOffer(
                    supplier_product_id=request.supplier_product_id,
                    price=None,
                    old_price=None,
                    available=False,
                    stock=0,
                    delivery_days=None,
                    seller=None,
                    adapter_schema_version=self.code,
                    observed_at=datetime.now(UTC),
                    currency="KZT",
                    raw_metadata={
                        "execution_surface": "local_http_agent",
                        "source": "otherOffersFromSellers.webSellerList",
                        "business_state": "no_active_seller_offers",
                        "offer_count": 0,
                        "product_id": result.get("product_id"),
                        "read_only": True,
                        "browser_used": False,
                    },
                )
            raise AdapterParseError("Ozon returned no confirmed KZT seller price", http_status=200)

        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=Decimal(price),
            old_price=None,
            available=True,
            stock=None,
            delivery_days=offer.get("delivery_days") if isinstance(offer.get("delivery_days"), int) else None,
            seller=(str(offer.get("seller_name") or "").strip() or None),
            adapter_schema_version=self.code,
            observed_at=datetime.now(UTC),
            currency="KZT",
            raw_metadata={
                "execution_surface": "local_http_agent",
                "source": "otherOffersFromSellers.webSellerList",
                "offer_count": int(result.get("other_offer_count") or 0),
                "offer_sku": offer.get("offer_sku"),
                "product_id": result.get("product_id"),
                "read_only": True,
                "browser_used": False,
            },
        )

    def _fetch_sync(self, url: str, external_id: str) -> dict[str, Any]:
        del external_id  # product id in the canonical URL is authoritative
        profile = self.resolver.resolve()
        client = OzonSessionHttpClient(profile)
        try:
            result: dict[str, Any] = {}
            for attempt in range(3):
                try:
                    result = client.product_price_hints(url)
                except Exception as exc:
                    name = type(exc).__name__.casefold()
                    transient = any(
                        marker in name
                        for marker in ("timeout", "connect", "network", "curl", "request")
                    )
                    if not transient or attempt == 2:
                        raise
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                status = int((result.get("attempt") or {}).get("status_code") or 0)
                if status not in {408, 425, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}:
                    return result
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
            return result
        finally:
            client.close()

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            host in {"ozon.ru", "ozon.kz"} or host.endswith(".ozon.ru") or host.endswith(".ozon.kz")
        ):
            raise ValueError("Ozon HTTP adapter accepts only HTTPS ozon.ru/ozon.kz URLs")
