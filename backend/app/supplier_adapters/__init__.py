"""Supplier adapter contracts and implementations."""

from .base import AdapterRequest, NormalizedOffer, SupplierAdapter
from .errors import AdapterError
from .ozon_http import OzonHttpAdapter

__all__ = [
    "AdapterError",
    "AdapterRequest",
    "NormalizedOffer",
    "OzonHttpAdapter",
    "SupplierAdapter",
]
