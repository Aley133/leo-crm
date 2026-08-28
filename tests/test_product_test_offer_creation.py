import asyncio

import pytest

from tools import product_test_agent
from tools.product_discovery.kaspi_offer_creator import MerchantOfferApi, OfferState


def test_existing_zero_day_offer_is_repaired_without_creating_a_duplicate(monkeypatch) -> None:
    api = object.__new__(MerchantOfferApi)
    states = iter([
        OfferState(
            found=True,
            sku="138791468_857843219",
            master_sku="138791468",
            stock_count=5,
            preorder_days=0,
            price_kzt=9599,
        ),
        OfferState(
            found=True,
            sku="138791468_857843219",
            master_sku="138791468",
            stock_count=5,
            preorder_days=1,
            price_kzt=9599,
        ),
    ])
    writes: list[dict] = []
    monkeypatch.setattr(api, "read_offer", lambda _reference: next(states))
    monkeypatch.setattr(
        api,
        "process_offer",
        lambda **kwargs: writes.append(kwargs) or {"accepted": True, "status_code": 200},
    )

    result = api.create_linked_offer(
        master_sku="138791468",
        model="GLS Omega-3",
        price=9599,
        stock=5,
        preorder=0,
        live=True,
        attempts=1,
        poll_seconds=0.5,
    )

    assert result["result"] == "ALREADY_EXISTS"
    assert result["merchant_sku"] == "138791468_857843219"
    assert result["after"]["preorder_days"] == 1
    assert result["preorder_verified"] is True
    assert writes == [{
        "sku": "138791468_857843219",
        "model": "GLS Omega-3",
        "price": 9599,
        "stock": 5,
        "preorder": 1,
        "live": True,
    }]


def test_existing_offer_is_not_reported_as_success_before_values_are_confirmed(monkeypatch) -> None:
    api = object.__new__(MerchantOfferApi)
    stale = OfferState(
        found=True,
        sku="138791468_857843219",
        master_sku="138791468",
        stock_count=5,
        preorder_days=0,
        price_kzt=9599,
    )
    monkeypatch.setattr(api, "read_offer", lambda _reference: stale)
    monkeypatch.setattr(api, "process_offer", lambda **_kwargs: {"accepted": True, "status_code": 200})

    result = api.create_linked_offer(
        master_sku="138791468",
        model="GLS Omega-3",
        price=9599,
        stock=5,
        preorder=1,
        live=True,
        attempts=1,
        poll_seconds=0.5,
    )

    assert result["result"] == "EXISTING_PROCESS_ACCEPTED_NOT_CONFIRMED"
    assert result["after"]["preorder_days"] == 0


def test_product_test_agent_uses_full_confirmation_window(monkeypatch) -> None:
    captured: dict = {}

    class FakeMerchantOfferApi:
        def __init__(self, _session, *, store_id: str, city_id: str) -> None:
            captured["store_id"] = store_id
            captured["city_id"] = city_id

        def create_linked_offer(self, **kwargs):
            captured.update(kwargs)
            return {
                "result": "ALREADY_EXISTS",
                "merchant_sku": "138791468_857843219",
                "after": {
                    "found": True,
                    "sku": "138791468_857843219",
                    "price_kzt": 9599,
                    "stock_count": 5,
                    "preorder_days": 1,
                },
            }

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", FakeMerchantOfferApi)
    result = asyncio.run(product_test_agent._execute_job(
        {
            "job_type": "create_offer",
            "city_id": "196220100",
            "options": {
                "master_sku": "138791468",
                "model": "GLS Omega-3",
                "initial_price_kzt": 9599,
                "stock_count": 5,
                "preorder_days": 1,
            },
        },
        merchant_session=object(),
        store_id="11843018_041600",
    ))

    assert result["result"] == "ALREADY_EXISTS"
    assert captured["attempts"] == 180
    assert captured["poll_seconds"] == 5.0


def test_product_test_agent_rejects_zero_day_confirmation(monkeypatch) -> None:
    class FakeMerchantOfferApi:
        def __init__(self, _session, *, store_id: str, city_id: str) -> None:
            pass

        def create_linked_offer(self, **_kwargs):
            return {
                "result": "ALREADY_EXISTS",
                "merchant_sku": "138791468_857843219",
                "after": {
                    "found": True,
                    "sku": "138791468_857843219",
                    "price_kzt": 9599,
                    "stock_count": 5,
                    "preorder_days": 0,
                },
            }

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", FakeMerchantOfferApi)
    with pytest.raises(RuntimeError, match="предзаказ"):
        asyncio.run(product_test_agent._execute_job(
            {
                "job_type": "create_offer",
                "city_id": "196220100",
                "options": {
                    "master_sku": "138791468",
                    "model": "GLS Omega-3",
                    "initial_price_kzt": 9599,
                    "stock_count": 5,
                    "preorder_days": 1,
                },
            },
            merchant_session=object(),
            store_id="11843018_041600",
        ))
