from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend.app.kaspi_http_transport import (
    DEFAULT_KASPI_API_BASE_URL,
    DEFAULT_KASPI_API_TIMEOUT_SECONDS,
    DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS,
    KaspiAuthenticationError,
    KaspiConfigurationError,
    KaspiHttpSettings,
    KaspiHttpTransport,
    KaspiRateLimitError,
    KaspiTemporaryError,
    KaspiTransportError,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_environment_configuration_is_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("KASPI_API_TOKEN", raising=False)
    with pytest.raises(KaspiConfigurationError, match="KASPI_API_TOKEN"):
        KaspiHttpSettings.from_environment()


def test_environment_configuration_uses_stable_defaults(monkeypatch) -> None:
    monkeypatch.setenv("KASPI_API_TOKEN", "secret")
    monkeypatch.delenv("KASPI_API_BASE_URL", raising=False)
    monkeypatch.delenv("KASPI_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("KASPI_INITIAL_LOOKBACK_DAYS", raising=False)

    settings = KaspiHttpSettings.from_environment()

    assert settings.api_token == "secret"
    assert settings.base_url == DEFAULT_KASPI_API_BASE_URL
    assert settings.timeout_seconds == DEFAULT_KASPI_API_TIMEOUT_SECONDS
    assert settings.initial_lookback_days == DEFAULT_KASPI_INITIAL_LOOKBACK_DAYS


def test_fetch_orders_builds_bounded_official_json_api_request_and_parses_page() -> None:
    seen: dict = {}
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "orders",
                        "id": "order-1",
                        "attributes": {
                            "code": "996801988",
                            "status": "NEW",
                            "updatedAt": "2026-07-19T10:05:00Z",
                        },
                    }
                ],
                "links": {
                    "next": "https://kaspi.kz/shop/api/v2/orders?page%5Bnumber%5D=3&page%5Bsize%5D=1"
                },
            },
        )

    transport = KaspiHttpTransport(
        KaspiHttpSettings(api_token="secret"),
        client=_client(handler),
        clock=lambda: now,
    )
    page = transport.fetch_orders(
        cursor="2",
        updated_after=datetime(2026, 7, 18, 10, 0, tzinfo=UTC),
        limit=1,
    )

    request = seen["request"]
    assert request.headers["X-Auth-Token"] == "secret"
    assert request.headers["Accept"] == "application/vnd.api+json"
    assert request.url.params["page[number]"] == "2"
    assert request.url.params["page[size]"] == "1"
    assert request.url.params["filter[orders][creationDate][$ge]"] == "1784368800000"
    assert request.url.params["filter[orders][creationDate][$le]"] == str(
        int(now.timestamp() * 1000)
    )
    assert page.items[0]["id"] == "order-1"
    assert page.next_cursor == "3"
    assert page.watermark_at == datetime(2026, 7, 19, 10, 5, tzinfo=UTC)


def test_initial_request_uses_configured_lookback_window() -> None:
    seen: dict = {}
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"data": [], "links": {"next": None}})

    settings = KaspiHttpSettings(api_token="secret", initial_lookback_days=3)
    transport = KaspiHttpTransport(
        settings,
        client=_client(handler),
        clock=lambda: now,
    )

    transport.fetch_orders(cursor=None, updated_after=None, limit=10)

    params = seen["request"].url.params
    assert params["filter[orders][creationDate][$ge]"] == str(
        int((now - timedelta(days=3)).timestamp() * 1000)
    )
    assert params["filter[orders][creationDate][$le]"] == str(int(now.timestamp() * 1000))


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, KaspiAuthenticationError),
        (403, KaspiAuthenticationError),
        (429, KaspiRateLimitError),
        (503, KaspiTemporaryError),
        (400, KaspiTransportError),
    ],
)
def test_http_failures_are_classified(status: int, error_type: type[Exception]) -> None:
    client = _client(lambda request: httpx.Response(status, json={"errors": []}))
    transport = KaspiHttpTransport(KaspiHttpSettings(api_token="secret"), client=client)
    with pytest.raises(error_type):
        transport.fetch_orders(cursor=None, updated_after=None, limit=20)


def test_invalid_json_api_shape_is_rejected() -> None:
    client = _client(lambda request: httpx.Response(200, json={"data": {}}))
    transport = KaspiHttpTransport(KaspiHttpSettings(api_token="secret"), client=client)
    with pytest.raises(KaspiTransportError, match="data list"):
        transport.fetch_orders(cursor=None, updated_after=None, limit=20)


def test_page_size_is_bounded_by_kaspi_limit() -> None:
    client = _client(lambda request: httpx.Response(200, json={"data": []}))
    transport = KaspiHttpTransport(KaspiHttpSettings(api_token="secret"), client=client)
    with pytest.raises(ValueError, match="between 1 and 100"):
        transport.fetch_orders(cursor=None, updated_after=None, limit=101)
