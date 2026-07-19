from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .marketplace_transport import MarketplaceOrderPage

DEFAULT_KASPI_API_BASE_URL = "https://kaspi.kz/shop/api/v2"
DEFAULT_KASPI_API_TIMEOUT_SECONDS = 80.0
DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS = 7


class KaspiConfigurationError(RuntimeError):
    pass


class KaspiTransportError(RuntimeError):
    pass


class KaspiAuthenticationError(KaspiTransportError):
    pass


class KaspiRateLimitError(KaspiTransportError):
    pass


class KaspiTemporaryError(KaspiTransportError):
    pass


@dataclass(frozen=True, slots=True)
class KaspiHttpSettings:
    api_token: str
    base_url: str = DEFAULT_KASPI_API_BASE_URL
    timeout_seconds: float = DEFAULT_KASPI_API_TIMEOUT_SECONDS
    initial_lookback_days: int = DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS

    @classmethod
    def from_environment(cls) -> "KaspiHttpSettings":
        token = os.getenv("KASPI_API_TOKEN", "").strip()
        if not token:
            raise KaspiConfigurationError("KASPI_API_TOKEN is not configured")
        base_url = os.getenv("KASPI_API_BASE_URL", DEFAULT_KASPI_API_BASE_URL).strip().rstrip("/")
        if not base_url:
            raise KaspiConfigurationError("KASPI_API_BASE_URL must not be empty")
        try:
            timeout = float(
                os.getenv(
                    "KASPI_API_TIMEOUT_SECONDS",
                    str(DEFAULT_KASPI_API_TIMEOUT_SECONDS),
                ).strip()
            )
        except ValueError as exc:
            raise KaspiConfigurationError("KASPI_API_TIMEOUT_SECONDS must be numeric") from exc
        if timeout <= 0 or timeout > 180:
            raise KaspiConfigurationError("KASPI_API_TIMEOUT_SECONDS must be between 0 and 180")
        try:
            lookback_days = int(
                os.getenv(
                    "KASPI_INITIAL_LOOKBACK_DAYS",
                    str(DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS),
                ).strip()
            )
        except ValueError as exc:
            raise KaspiConfigurationError("KASPI_INITIAL_LOOKBACK_DAYS must be an integer") from exc
        if lookback_days < 1 or lookback_days > 90:
            raise KaspiConfigurationError("KASPI_INITIAL_LOOKBACK_DAYS must be between 1 and 90")
        return cls(token, base_url, timeout, lookback_days)


class KaspiHttpTransport:
    def __init__(
        self,
        settings: KaspiHttpSettings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not settings.api_token.strip():
            raise KaspiConfigurationError("Kaspi API token is empty")
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.timeout_seconds,
                write=20.0,
                pool=60.0,
            )
        )
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(cls) -> "KaspiHttpTransport":
        return cls(KaspiHttpSettings.from_environment())

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_orders(
        self,
        *,
        cursor: str | None,
        updated_after: datetime | None,
        limit: int,
    ) -> MarketplaceOrderPage:
        if limit < 1 or limit > 100:
            raise ValueError("Kaspi page size must be between 1 and 100")
        page_number = self._parse_cursor(cursor)
        window_end = self._as_utc(self._clock())
        window_start = (
            self._as_utc(updated_after)
            if updated_after is not None
            else window_end - timedelta(days=self._settings.initial_lookback_days)
        )
        if window_start > window_end:
            raise ValueError("Kaspi order query start must not be after query end")
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(window_end.timestamp() * 1000)
        params: dict[str, str | int] = {
            "include": "entries",
            "page[number]": page_number,
            "page[size]": limit,
            "filter[orders][by]": "creationDate",
            "filter[orders][creationDate][$ge]": start_ms,
            "filter[orders][creationDate][$le]": end_ms,
            "filter[orders][date][$ge]": start_ms,
            "filter[orders][date][$le]": end_ms,
        }
        document = self._get_json("/orders", params=params)
        if not isinstance(document.get("data"), list):
            raise KaspiTransportError("Kaspi JSON:API response has no data list")

        included_index = self._index_included(document.get("included"))
        items = tuple(
            self._hydrate_order(item, included_index)
            for item in document["data"]
            if isinstance(item, dict)
        )
        return MarketplaceOrderPage(
            items=items,
            next_cursor=self._next_cursor(document, page_number, len(items), limit),
            watermark_at=self._watermark(items),
        )

    def _get_json(self, path: str, *, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self._settings.base_url}{path}",
                params=params,
                headers={
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                    "X-Auth-Token": self._settings.api_token,
                    "User-Agent": "leo-crm/0.8.2",
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise KaspiTemporaryError(f"Kaspi request failed: {exc}") from exc
        if response.status_code in {401, 403}:
            raise KaspiAuthenticationError(
                f"Kaspi authentication failed with HTTP {response.status_code}"
            )
        if response.status_code == 429:
            raise KaspiRateLimitError("Kaspi rate limit exceeded")
        if response.status_code >= 500:
            raise KaspiTemporaryError(f"Kaspi temporary HTTP {response.status_code}")
        if response.status_code >= 400:
            raise KaspiTransportError(
                f"Kaspi request rejected with HTTP {response.status_code}"
            )
        try:
            document = response.json()
        except ValueError as exc:
            raise KaspiTransportError("Kaspi returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise KaspiTransportError("Kaspi JSON:API response must be an object")
        return document

    def _hydrate_order(
        self,
        item: dict[str, Any],
        included_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        order = dict(item)
        attributes = dict(item.get("attributes") or {})
        entries = self._entries_from_order_relationship(item, included_index)
        if not entries:
            order_id = str(item.get("id") or "").strip()
            if order_id:
                entries = self._fetch_order_entries(order_id)
        attributes["entries"] = entries
        order["attributes"] = attributes
        return order

    def _fetch_order_entries(self, order_id: str) -> list[dict[str, Any]]:
        document = self._get_json(
            f"/orders/{order_id}/entries",
            params={
                "page[size]": 200,
                "include": "product,merchantProduct,masterProduct",
            },
        )
        data = document.get("data")
        if not isinstance(data, list):
            raise KaspiTransportError("Kaspi order entries response has no data list")
        included_index = self._index_included(document.get("included"))
        return [
            self._flatten_entry(entry, included_index)
            for entry in data
            if isinstance(entry, dict)
        ]

    @classmethod
    def _entries_from_order_relationship(
        cls,
        order: dict[str, Any],
        included_index: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        relationships = order.get("relationships")
        entries_rel = relationships.get("entries") if isinstance(relationships, dict) else None
        refs = entries_rel.get("data") if isinstance(entries_rel, dict) else None
        if not isinstance(refs, list):
            return []
        entries: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            key = (str(ref.get("type") or ""), str(ref.get("id") or ""))
            entry = included_index.get(key)
            if entry is not None:
                entries.append(cls._flatten_entry(entry, included_index))
        return entries

    @staticmethod
    def _index_included(value: Any) -> dict[tuple[str, str], dict[str, Any]]:
        if not isinstance(value, list):
            return {}
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            item_id = str(item.get("id") or "")
            if item_type and item_id:
                index[(item_type, item_id)] = item
        return index

    @classmethod
    def _flatten_entry(
        cls,
        entry: dict[str, Any],
        included_index: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        flattened = dict(entry)
        attrs = dict(entry.get("attributes") or {})
        relationships = entry.get("relationships")
        relationships = relationships if isinstance(relationships, dict) else {}

        title_candidates = [attrs.get("name"), attrs.get("title")]
        sku_candidates = [attrs.get("offerCode"), attrs.get("merchantSku"), attrs.get("sku")]
        external_product_id = attrs.get("productId") or attrs.get("externalProductId")

        offer = attrs.get("offer")
        if isinstance(offer, dict):
            title_candidates.append(offer.get("name"))
            sku_candidates.append(offer.get("code"))

        for relation_name in ("merchantProduct", "product", "masterProduct"):
            relation = relationships.get(relation_name)
            ref = relation.get("data") if isinstance(relation, dict) else None
            if not isinstance(ref, dict):
                continue
            ref_type = str(ref.get("type") or "")
            ref_id = str(ref.get("id") or "")
            if not external_product_id and ref_id:
                external_product_id = ref_id
            included = included_index.get((ref_type, ref_id), {})
            included_attrs = included.get("attributes") if isinstance(included, dict) else None
            if isinstance(included_attrs, dict):
                title_candidates.extend(
                    [included_attrs.get("name"), included_attrs.get("title")]
                )
                sku_candidates.extend(
                    [
                        included_attrs.get("code"),
                        included_attrs.get("sku"),
                        included_attrs.get("offerCode"),
                    ]
                )

        title = next(
            (str(value).strip() for value in title_candidates if value not in (None, "") and str(value).strip()),
            "Unknown product",
        )
        merchant_sku = next(
            (str(value).strip() for value in sku_candidates if value not in (None, "") and str(value).strip()),
            None,
        )
        attrs["name"] = title
        if merchant_sku is not None:
            attrs["offerCode"] = merchant_sku
        if external_product_id is not None:
            attrs["productId"] = str(external_product_id)
        flattened["attributes"] = attrs
        return flattened

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _parse_cursor(cursor: str | None) -> int:
        if not cursor:
            return 1
        try:
            value = int(cursor)
        except ValueError as exc:
            raise ValueError("Kaspi cursor must be a positive page number") from exc
        if value < 1:
            raise ValueError("Kaspi cursor must be a positive page number")
        return value

    @staticmethod
    def _next_cursor(
        document: dict[str, Any],
        current_page: int,
        item_count: int,
        limit: int,
    ) -> str | None:
        links = document.get("links")
        if isinstance(links, dict):
            next_link = links.get("next")
            if isinstance(next_link, str) and next_link:
                values = parse_qs(urlparse(next_link).query).get("page[number]")
                if values and values[0].isdigit():
                    return values[0]
            if next_link is None:
                return None
        meta = document.get("meta")
        if isinstance(meta, dict) and meta.get("pageCount") is not None:
            try:
                page_count = int(meta["pageCount"])
            except (TypeError, ValueError):
                page_count = 0
            return str(current_page + 1) if current_page < page_count else None
        return str(current_page + 1) if item_count == limit else None

    @staticmethod
    def _watermark(items: tuple[dict[str, Any], ...]) -> datetime | None:
        values: list[datetime] = []
        for item in items:
            attrs = item.get("attributes")
            if not isinstance(attrs, dict):
                continue
            raw = attrs.get("updatedAt") or attrs.get("modifiedAt")
            try:
                if isinstance(raw, (int, float)):
                    ts = float(raw) / 1000 if float(raw) > 10_000_000_000 else float(raw)
                    values.append(datetime.fromtimestamp(ts, tz=UTC))
                elif isinstance(raw, str):
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    values.append(
                        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
                    )
            except (ValueError, OverflowError):
                continue
        return max(values) if values else None
