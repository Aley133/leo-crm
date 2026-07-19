from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.browser_runtime import (
    BrowserFailureCode,
    BrowserRequest,
    BrowserResponse,
    BrowserRuntimeError,
    FakeBrowserRuntime,
)
from backend.app.supplier_adapters.base import AccessStrategy, AdapterRequest
from backend.app.supplier_adapters.errors import AdapterCaptchaError, AdapterTimeoutError
from backend.app.supplier_adapters.ozon_browser import OzonBrowserAdapter


def test_browser_request_rejects_unsafe_or_unbounded_input() -> None:
    with pytest.raises(ValueError):
        BrowserRequest(url="javascript:alert(1)", operation="test")
    with pytest.raises(ValueError):
        BrowserRequest(url="https://ozon.ru/product/1", operation="", timeout_seconds=10)
    with pytest.raises(ValueError):
        BrowserRequest(url="https://ozon.ru/product/1", operation="test", timeout_seconds=0)


def test_fake_runtime_records_request_and_returns_deterministic_response() -> None:
    response = BrowserResponse(
        final_url="https://www.ozon.ru/product/example-1/",
        content="<html></html>",
        observed_at=datetime.now(UTC),
        duration_ms=25,
        runtime_id="fake",
        session_id="session-1",
    )
    runtime = FakeBrowserRuntime([response])
    request = BrowserRequest(
        url="https://www.ozon.ru/product/example-1/",
        operation="ozon.product.fetch",
    )

    result = asyncio.run(runtime.execute(request))

    assert result is response
    assert runtime.requests == [request]


def test_ozon_browser_adapter_implements_existing_supplier_contract() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "Product",
        "name": "Omega 3",
        "offers": {
          "price": "3734",
          "priceCurrency": "RUB",
          "availability": "https://schema.org/InStock",
          "seller": {"name": "Ozon"}
        }
      }
      </script>
    </head></html>
    """
    runtime = FakeBrowserRuntime(
        [
            BrowserResponse(
                final_url="https://www.ozon.ru/product/omega-3-1/",
                content=html,
                observed_at=datetime.now(UTC),
                duration_ms=120,
                runtime_id="fake",
            )
        ]
    )
    adapter = OzonBrowserAdapter(runtime)

    offer = asyncio.run(
        adapter.fetch(
            AdapterRequest(
                supplier_product_id=17,
                url="https://www.ozon.ru/product/omega-3-1/",
                external_id="omega-3-1",
            )
        )
    )

    assert adapter.access_strategy == AccessStrategy.BROWSER
    assert offer.supplier_product_id == 17
    assert offer.price == Decimal("3734")
    assert offer.available is True
    assert offer.seller == "Ozon"
    assert offer.raw_metadata["runtime_id"] == "fake"
    assert runtime.requests[0].session_key == "ozon"


def test_ozon_browser_adapter_maps_runtime_failures_to_monitoring_errors() -> None:
    request = AdapterRequest(
        supplier_product_id=17,
        url="https://www.ozon.ru/product/omega-3-1/",
        external_id="omega-3-1",
    )

    timeout_adapter = OzonBrowserAdapter(
        FakeBrowserRuntime(
            [BrowserRuntimeError(BrowserFailureCode.TIMEOUT, "deadline", retryable=True)]
        )
    )
    with pytest.raises(AdapterTimeoutError):
        asyncio.run(timeout_adapter.fetch(request))

    captcha_adapter = OzonBrowserAdapter(
        FakeBrowserRuntime(
            [BrowserRuntimeError(BrowserFailureCode.CAPTCHA, "captcha", retryable=False)]
        )
    )
    with pytest.raises(AdapterCaptchaError):
        asyncio.run(captcha_adapter.fetch(request))
