from __future__ import annotations

import asyncio

from backend.app import monitoring_api
from backend.app.scheduler_engine import AdapterRegistry
from backend.app.supplier_adapters.base import AccessStrategy
from tools.ozon_http import OzonSessionHttpAdapter


class ClosableAdapter:
    code = "test-browser"
    access_strategy = AccessStrategy.BROWSER

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_runtime_registry_uses_fast_ozon_http_session_adapter() -> None:
    registry = monitoring_api._runtime_registry()
    adapter = registry.get("ozon")

    assert isinstance(adapter, OzonSessionHttpAdapter)
    assert adapter.access_strategy == AccessStrategy.DIRECT_HTTP

    asyncio.run(monitoring_api._close_runtime_registry(registry))


def test_runtime_registry_cleanup_closes_adapter() -> None:
    adapter = ClosableAdapter()
    registry = AdapterRegistry({"ozon": adapter})

    asyncio.run(monitoring_api._close_runtime_registry(registry))

    assert adapter.closed is True
