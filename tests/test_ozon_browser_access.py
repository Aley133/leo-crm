import asyncio

import pytest

from backend.app.monitoring_api import _runtime_registry
from backend.app.supplier_adapters.base import AdapterRequest
from backend.app.supplier_adapters.errors import AdapterBlockedError
from backend.app.supplier_adapters.ozon_browser_access import OzonBrowserAccessAdapter
from backend.app.supplier_adapters.playwright_pool import BrowserPageResult


class StubPool:
    async def fetch_html(self, url: str, *, timeout_seconds: float) -> BrowserPageResult:
        return BrowserPageResult(
            final_url=url,
            content="<html><head><script>window.challenge=true</script></head></html>",
            duration_ms=15,
            title="Похоже, нет соединения",
            body_text="Похоже, нет соединения в интернет. Обновить страницу.",
        )

    async def close(self) -> None:
        return None


def test_ozon_challenge_shell_is_blocked_not_parse_error() -> None:
    adapter = OzonBrowserAccessAdapter(StubPool())
    request = AdapterRequest(
        supplier_product_id=1,
        url="https://www.ozon.ru/product/608450235/",
        external_id="608450235",
    )

    with pytest.raises(AdapterBlockedError) as exc_info:
        asyncio.run(adapter.fetch(request))

    message = str(exc_info.value)
    assert "anti-bot challenge" in message
    assert "title=Похоже, нет соединения" in message
    assert "challenge=challenge" in message


def test_runtime_registry_uses_antibot_aware_adapter() -> None:
    registry = _runtime_registry()
    adapter = registry.get("ozon")
    assert adapter.code == "ozon-browser-v6"
