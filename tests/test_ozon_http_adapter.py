from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from backend.app.monitoring import AttemptOutcome
from backend.app.scheduler_engine import classify_adapter_exception
from backend.app.supplier_adapters.base import AdapterRequest
from backend.app.supplier_adapters.errors import (
    AdapterCaptchaError,
    AdapterNotFoundError,
    AdapterParseError,
    AdapterRateLimitedError,
    AdapterTimeoutError,
)
from backend.app.supplier_adapters.ozon_http import OzonHttpAdapter


def _request(url: str = "https://www.ozon.ru/product/test-product-123/") -> AdapterRequest:
    return AdapterRequest(
        supplier_product_id=77,
        url=url,
        external_id="123",
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _product_html(*, currency: str = "RUB") -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Test product",
        "offers": {
            "@type": "Offer",
            "price": "5640.00",
            "priceCurrency": currency,
            "availability": "https://schema.org/InStock",
            "seller": {"@type": "Organization", "name": "Ozon seller"},
        },
    }
    return (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(payload)
        + "</script></head></html>"
    )


def test_ozon_http_adapter_parses_reliable_json_ld_offer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_product_html(), request=request)

    async def scenario() -> None:
        async with _client(handler) as client:
            offer = await OzonHttpAdapter(client=client).fetch(_request())
        assert offer.price == Decimal("5640.00")
        assert offer.available is True
        assert offer.seller == "Ozon seller"
        assert offer.adapter_schema_version == "ozon-jsonld-v1"
        assert offer.raw_metadata["currency"] == "RUB"

    asyncio.run(scenario())


def test_ozon_http_adapter_accepts_ozon_kz_product_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ozon.kz"
        return httpx.Response(200, text=_product_html(currency="KZT"), request=request)

    async def scenario() -> None:
        async with _client(handler) as client:
            offer = await OzonHttpAdapter(client=client).fetch(
                _request("https://ozon.kz/product/test-product-123/")
            )
        assert offer.price == Decimal("5640.00")
        assert offer.raw_metadata["currency"] == "KZT"

    asyncio.run(scenario())


def test_ozon_http_adapter_classifies_captcha_even_with_http_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body>Подтвердите, что вы не робот — captcha</body></html>",
            request=request,
        )

    async def scenario() -> None:
        async with _client(handler) as client:
            with pytest.raises(AdapterCaptchaError):
                await OzonHttpAdapter(client=client).fetch(_request())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "exception_type", "outcome"),
    [
        (404, AdapterNotFoundError, AttemptOutcome.NOT_FOUND),
        (429, AdapterRateLimitedError, AttemptOutcome.RATE_LIMITED),
    ],
)
def test_ozon_http_adapter_classifies_http_failures(
    status_code: int,
    exception_type: type[Exception],
    outcome: AttemptOutcome,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="failure", request=request)

    async def scenario() -> None:
        async with _client(handler) as client:
            with pytest.raises(exception_type) as raised:
                await OzonHttpAdapter(client=client).fetch(_request())
        classified, error_code, http_status = classify_adapter_exception(raised.value)
        assert classified is outcome
        assert error_code.startswith("adapter_")
        assert http_status == status_code

    asyncio.run(scenario())


def test_ozon_http_adapter_rejects_unstructured_success_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>ordinary page</body></html>", request=request)

    async def scenario() -> None:
        async with _client(handler) as client:
            with pytest.raises(AdapterParseError):
                await OzonHttpAdapter(client=client).fetch(_request())

    asyncio.run(scenario())


def test_ozon_http_adapter_maps_httpx_timeout_to_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    async def scenario() -> None:
        async with _client(handler) as client:
            with pytest.raises(AdapterTimeoutError) as raised:
                await OzonHttpAdapter(client=client).fetch(_request())
        outcome, error_code, http_status = classify_adapter_exception(raised.value)
        assert outcome is AttemptOutcome.TIMEOUT
        assert error_code == "adapter_timeout"
        assert http_status is None

    asyncio.run(scenario())


def test_ozon_http_adapter_rejects_non_ozon_url() -> None:
    async def scenario() -> None:
        async with _client(lambda request: httpx.Response(200, request=request)) as client:
            with pytest.raises(ValueError, match="ozon.ru or ozon.kz"):
                await OzonHttpAdapter(client=client).fetch(
                    AdapterRequest(1, "https://example.com/product/1", "1")
                )

    asyncio.run(scenario())
