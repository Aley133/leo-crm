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


def test_fetch_orders_builds_proven_bounded_request_and_hydrates_included_entries() -> None:
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
                        "relationships": {
                            "entries": {
                                "data": [{"type": "orderentries", "id": "entry-1"}]
                            }
                        },
                    }
                ],
                "included": [
                    {
                        "type": "orderentries",
                        "id": "entry-1",
                        "attributes": {
                            "quantity": 2,
                            "basePrice": 4500,
                            "totalPrice": 9000,
                            "offer": {"code": "SKU-1", "name": "Vitamin D3"},
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
    assert request.headers["User-Agent"] == "leo-crm/0.8.2"
    assert request.url.params["include"] == "entries"
    assert request.url.params["page[number]"] == "2"
    assert request.url.params["page[size]"] == "1"
    assert request.url.params["filter[orders][by]"] == "creationDate"
    assert request.url.params["filter[orders][creationDate][$ge]"] == "1784368800000"
    assert request.url.params["filter[orders][creationDate][$le]"] == str(
        int(now.timestamp() * 1000)
    )
    assert request.url.params["filter[orders][date][$ge]"] == "1784368800000"
    assert request.url.params["filter[orders][date][$le]"] == str(
        int(now.timestamp() * 1000)
    )
    assert page.items[0]["id"] == "order-1"
    entry = page.items[0]["attributes"]["entries"][0]
    assert entry["id"] == "entry-1"
    assert entry["attributes"]["offerCode"] == "SKU-1"
    assert entry["attributes"]["name"] == "Vitamin D3"
    assert page.next_cursor == "3"
    assert page.watermark_at == datetime(2026, 7, 19, 10, 5, tzinfo=UTC)


def test_fetch_order_by_code_uses_official_filter_without_date_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/orders"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "orders",
                            "id": "order-1",
                            "attributes": {
                                "code": "1002303844",
                                "status": "ACCEPTED_BY_MERCHANT",
                            },
                            "relationships": {"entries": {"data": []}},
                        }
                    ],
                    "included": [],
                },
            )
        assert request.url.path.endswith("/orders/order-1/entries")
        return httpx.Response(200, json={"data": [], "included": []})

    transport = KaspiHttpTransport(
        KaspiHttpSettings(api_token="secret"),
        client=_client(handler),
    )
    order = transport.fetch_order_by_code("1002303844")

    order_request = requests[0]
    assert order_request.url.path.endswith("/orders")
    assert order_request.url.params["filter[orders][code]"] == "1002303844"
    assert order_request.url.params["page[number]"] == "0"
    assert order_request.url.params["page[size]"] == "1"
    assert "filter[orders][creationDate][$ge]" not in order_request.url.params
    assert order is not None
    assert order["attributes"]["code"] == "1002303844"


def test_fetch_orders_falls_back_to_order_entries_subresource() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/orders"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "orders",
                            "id": "order-1",
                            "attributes": {"code": "996801988", "status": "NEW"},
                        }
                    ],
                    "links": {"next": None},
                },
            )
        assert request.url.path.endswith("/orders/order-1/entries")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "orderentries",
                        "id": "entry-1",
                        "attributes": {"quantity": 1, "basePrice": 5200},
                        "relationships": {
                            "merchantProduct": {
                                "data": {"type": "merchantProducts", "id": "merchant-1"}
                            }
                        },
                    }
                ],
                "included": [
                    {
                        "type": "merchantProducts",
                        "id": "merchant-1",
                        "attributes": {"code": "SKU-5200", "name": "Omega 3"},
                    }
                ],
            },
        )

    transport = KaspiHttpTransport(
        KaspiHttpSettings(api_token="secret"),
        client=_client(handler),
    )
    page = transport.fetch_orders(cursor=None, updated_after=None, limit=10)

    assert len(requests) == 2
    entry = page.items[0]["attributes"]["entries"][0]
    assert entry["attributes"]["offerCode"] == "SKU-5200"
    assert entry["attributes"]["name"] == "Omega 3"
    assert entry["attributes"]["productId"] == "merchant-1"


def test_initial_request_uses_production_one_based_page_and_configured_lookback_window() -> None:
    seen: dict = {}
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"data": [], "links": {"next": None}})

    transport = KaspiHttpTransport(
        KaspiHttpSettings(api_token="secret", initial_lookback_days=3),
        client=_client(handler),
        clock=lambda: now,
    )
    transport.fetch_orders(cursor=None, updated_after=None, limit=10)

    params = seen["request"].url.params
    assert params["page[number]"] == "1"
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
