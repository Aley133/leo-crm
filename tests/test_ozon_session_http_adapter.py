from __future__ import annotations

import asyncio

import pytest

from backend.app.supplier_adapters.base import AccessStrategy, AdapterRequest
from backend.app.supplier_adapters.errors import AdapterBlockedError, AdapterParseError
from tools.ozon_http import adapter as adapter_module
from tools.ozon_http.adapter import OzonSessionHttpAdapter


class FakeResolver:
    def resolve(self):
        return object()

    def invalidate(self):
        return None


class FakeClient:
    result = {}

    def __init__(self, _profile):
        pass

    def product_price_hints(self, url):
        assert url.endswith("-123456789/")
        return self.result

    def close(self):
        pass


def _request():
    return AdapterRequest(
        supplier_product_id=17,
        url="https://www.ozon.kz/product/example-123456789/",
        external_id="999999999",
    )


def test_session_adapter_returns_normalized_kzt_other_offer(monkeypatch) -> None:
    FakeClient.result = {
        "ok": True,
        "attempt": {"status_code": 200, "blocked": False},
        "product_id": "123456789",
        "other_offer_count": 2,
        "cheaper_price_kzt": 4990,
        "cheaper_offer": {"offer_sku": "offer-1", "seller_name": "Supplier", "delivery_days": 2},
    }
    monkeypatch.setattr(adapter_module, "OzonSessionHttpClient", FakeClient)
    adapter = OzonSessionHttpAdapter(FakeResolver())
    offer = asyncio.run(adapter.fetch(_request()))
    assert offer.price == 4990
    assert offer.currency == "KZT"
    assert offer.delivery_days == 2
    assert offer.access_strategy if hasattr(offer, "access_strategy") else True
    assert adapter.access_strategy == AccessStrategy.DIRECT_HTTP
    assert offer.raw_metadata["browser_used"] is False


def test_session_adapter_does_not_turn_missing_price_into_out_of_stock(monkeypatch) -> None:
    FakeClient.result = {"ok": True, "attempt": {"status_code": 200}, "cheaper_price_kzt": None}
    monkeypatch.setattr(adapter_module, "OzonSessionHttpClient", FakeClient)
    with pytest.raises(AdapterParseError):
        asyncio.run(OzonSessionHttpAdapter(FakeResolver()).fetch(_request()))


def test_session_adapter_accepts_authoritative_empty_seller_list_as_unavailable(monkeypatch) -> None:
    FakeClient.result = {
        "ok": True,
        "attempt": {"status_code": 200, "blocked": False},
        "product_id": "123456789",
        "other_offer_count": 0,
        "other_offers": [],
        "cheaper_price_kzt": None,
        "cheaper_offer": None,
    }
    monkeypatch.setattr(adapter_module, "OzonSessionHttpClient", FakeClient)
    offer = asyncio.run(OzonSessionHttpAdapter(FakeResolver()).fetch(_request()))
    assert offer.price is None
    assert offer.available is False
    assert offer.stock == 0
    assert offer.raw_metadata["business_state"] == "no_active_seller_offers"


def test_session_adapter_classifies_blocked_response(monkeypatch) -> None:
    FakeClient.result = {"ok": False, "attempt": {"status_code": 403, "blocked": True}}
    monkeypatch.setattr(adapter_module, "OzonSessionHttpClient", FakeClient)
    with pytest.raises(AdapterBlockedError):
        asyncio.run(OzonSessionHttpAdapter(FakeResolver()).fetch(_request()))
