"""Supplier adapter contracts and implementations."""

from typing import Any

from .base import AdapterRequest, NormalizedOffer, SupplierAdapter
from .errors import AdapterError

__all__ = [
    "AdapterError",
    "AdapterRequest",
    "NormalizedOffer",
    "OzonBrowserAdapter",
    "OzonHttpAdapter",
    "SupplierAdapter",
]


def __getattr__(name: str) -> Any:
    """Keep legacy adapters import-compatible without loading browser code.

    The HTTP monitoring executable imports the shared offer contract from this
    package. Eagerly importing OzonBrowserAdapter used to pull the Playwright/CDP
    module into that process even though the HTTP runtime never selected it.
    """

    if name == "OzonBrowserAdapter":
        from .ozon_browser import OzonBrowserAdapter

        return OzonBrowserAdapter
    if name == "OzonHttpAdapter":
        from .ozon_http import OzonHttpAdapter

        return OzonHttpAdapter
    raise AttributeError(name)
