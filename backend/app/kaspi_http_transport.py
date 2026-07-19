from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .marketplace_transport import MarketplaceOrderPage


DEFAULT_KASPI_API_BASE_URL = "https://kaspi.kz/shop/api/v2"
DEFAULT_KASPI_API_TIMEOUT_SECONDS = 60.0
DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS = 7


class KaspiConfigurationError(RuntimeError):
    """Raised before any network call when Kaspi credentials are missing."""


class KaspiTransportError(RuntimeError):
    """Base error for classified Kaspi transport failures."""


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

        timeout_raw = os.getenv(
            "KASPI_API_TIMEOUT_SECONDS",
            str(DEFAULT_KASPI_API_TIMEOUT_SECONDS),
        ).strip()
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise KaspiConfigurationError("KASPI_API_TIMEOUT_SECONDS must be numeric") from exc
        if timeout <= 0 or timeout > 120:
            raise KaspiConfigurationError("KASPI_API_TIMEOUT_SECONDS must be between 0 and 120")

        lookback_raw = os.getenv(
            "KASPI_INITIAL_LOOKBACK_DAYS",
            str(DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS),
        ).strip()
        try:
            lookback_days = int(lookback_raw)
        except ValueError as exc:
            raise KaspiConfigurationError("KASPI_INITIAL_LOOKBACK_DAYS must be an integer") from exc
        if lookback_days < 1 or lookback_days > 90:
            raise KaspiConfigurationError("KASPI_INITIAL_LOOKBACK_DAYS must be between 1 and 90")

        return cls(
            api_token=token,
            base_url=base_url,
            timeout_seconds=timeout,
            initial_lookback_days=lookback_days,
        )


class KaspiHttpTransport:
    """Synchronous Kaspi order transport with no database responsibility.

    Every list request uses a bounded creation-date interval. Kaspi documents
    both lower and upper creationDate filters for order-list queries; sending an
    unbounded first request can be slow for established stores and may time out.
    Retries remain the responsibility of the outer scheduler so every attempt is
    represented by a database execution record.
    """

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
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
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

        params: dict[str, str | int] = {
            "page[number]": page_number,
            "page[size]": limit,
            "filter[orders][creationDate][$ge]": int(window_start.timestamp() * 1000),
            "filter[orders][creationDate][$le]": int(window_end.timestamp() * 1000),
        }

        try:
            response = self._client.get(
                f"{self._settings.base_url}/orders",
                params=params,
                headers={
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                    "X-Auth-Token": self._settings.api_token,
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
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            raise KaspiTransportError("Kaspi JSON:API response has no data list")

        items = tuple(item for item in document["data"] if isinstance(item, dict))
        next_cursor = self._next_cursor(
            document,
            current_page=page_number,
            item_count=len(items),
            limit=limit,
        )
        watermark = self._watermark(items)
        return MarketplaceOrderPage(
            items=items,
            next_cursor=next_cursor,
            watermark_at=watermark,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _parse_cursor(cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 0
        try:
            value = int(cursor)
        except ValueError as exc:
            raise ValueError("Kaspi cursor must be a non-negative page number") from exc
        if value < 0:
            raise ValueError("Kaspi cursor must be a non-negative page number")
        return value

    @staticmethod
    def _next_cursor(
        document: dict[str, Any],
        *,
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
        return str(current_page + 1) if item_count == limit else None

    @staticmethod
    def _watermark(items: tuple[dict[str, Any], ...]) -> datetime | None:
        values: list[datetime] = []
        for item in items:
            attributes = item.get("attributes")
            if not isinstance(attributes, dict):
                continue
            raw = attributes.get("updatedAt") or attributes.get("modifiedAt")
            if raw is None:
                continue
            try:
                if isinstance(raw, (int, float)):
                    timestamp = float(raw)
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    values.append(datetime.fromtimestamp(timestamp, tz=UTC))
                elif isinstance(raw, str):
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    values.append(
                        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
                    )
            except (ValueError, OverflowError):
                continue
        return max(values) if values else None
