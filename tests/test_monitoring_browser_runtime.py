from __future__ import annotations

import asyncio

from backend.app import monitoring_api
from backend.app.scheduler_engine import AdapterRegistry
from backend.app.supplier_adapters.base import AccessStrategy
from backend.app.supplier_adapters.ozon_browser import OzonBrowserAdapter


class ClosableAdapter:
    code = "test-browser"
    access_strategy = AccessStrategy.BROWSER

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_runtime_registry_uses_real_ozon_browser_adapter() -> None:
    registry = monitoring_api._runtime_registry()
    adapter = registry.get("ozon")

    assert isinstance(adapter, OzonBrowserAdapter)
    assert adapter.access_strategy == AccessStrategy.BROWSER

    asyncio.run(monitoring_api._close_runtime_registry(registry))


def test_runtime_registry_cleanup_closes_adapter() -> None:
    adapter = ClosableAdapter()
    registry = AdapterRegistry({"ozon": adapter})

    asyncio.run(monitoring_api._close_runtime_registry(registry))

    assert adapter.closed is True
