import asyncio
from types import SimpleNamespace

import pytest

from tools import product_test_agent
from tools.product_discovery.kaspi_offer_creator import MerchantOfferApi, OfferState


def test_product_test_agent_reprompts_after_invalid_ozon_session(monkeypatch) -> None:
    answers = iter(("curl invalid", "curl https://www.ozon.kz/search/?text=Solgar"))
    imports: list[str] = []
    messages: list[str] = []

    class Resolver:
        def resolve(self, *, validate=False):
            raise RuntimeError("saved session rejected")

        def import_curl(self, value, *, validate=False):
            imports.append(value)
            if value == "curl invalid":
                raise ValueError("invalid cURL")

    monkeypatch.setattr(product_test_agent, "OzonSessionResolver", Resolver)
    monkeypatch.setattr(
        product_test_agent,
        "_prompt_text",
        lambda *_args, **_kwargs: next(answers),
    )
    monkeypatch.setattr(
        product_test_agent,
        "_show_message",
        lambda _title, message, **_kwargs: messages.append(message),
    )

    product_test_agent._ensure_ozon_session()

    assert imports == [
        "curl invalid",
        "curl https://www.ozon.kz/search/?text=Solgar",
    ]
    assert len(messages) == 1
    assert "вставьте его ещё раз" in messages[0]


def test_product_test_agent_replaces_blocked_ozon_session_and_retries_job(monkeypatch) -> None:
    calls: list[str] = []
    refreshes: list[bool] = []

    async def execute(*_args, **_kwargs):
        calls.append("execute")
        if len(calls) == 1:
            raise product_test_agent.OzonSessionRefreshRequiredError("HTTP 403")
        return {"validated": True, "supplier_price_kzt": 2250}

    monkeypatch.setattr(product_test_agent, "_execute_job", execute)
    monkeypatch.setattr(
        product_test_agent,
        "_ensure_ozon_session",
        lambda *, force_replace=False: refreshes.append(force_replace),
    )
    monkeypatch.setattr(product_test_agent, "_log", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        product_test_agent._run_job_with_retry(
            {"job_type": "validate_supplier"},
            merchant_session=object(),
            store_id="store-1",
            workspace_id=1,
        )
    )

    assert result == {"validated": True, "supplier_price_kzt": 2250}
    assert calls == ["execute", "execute"]
    assert refreshes == [True]


def test_product_test_agent_reprompts_until_exact_ozon_job_succeeds(monkeypatch) -> None:
    calls: list[str] = []
    refreshes: list[bool] = []

    async def execute(*_args, **_kwargs):
        calls.append("execute")
        if len(calls) <= 3:
            raise product_test_agent.OzonSessionRefreshRequiredError("HTTP 403")
        return {"validated": True, "supplier_delivery_days": 2}

    monkeypatch.setattr(product_test_agent, "_execute_job", execute)
    monkeypatch.setattr(
        product_test_agent,
        "_ensure_ozon_session",
        lambda *, force_replace=False: refreshes.append(force_replace),
    )
    monkeypatch.setattr(product_test_agent, "_log", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        product_test_agent._run_job_with_retry(
            {"job_type": "validate_supplier"},
            merchant_session=object(),
            store_id="store-1",
            workspace_id=1,
        )
    )

    assert result == {"validated": True, "supplier_delivery_days": 2}
    assert calls == ["execute", "execute", "execute", "execute"]
    assert refreshes == [True, True, True]


def test_product_test_agent_stops_when_operator_cancels_ozon_refresh(monkeypatch) -> None:
    async def execute(*_args, **_kwargs):
        raise product_test_agent.OzonSessionRefreshRequiredError("HTTP 403")

    def cancel_refresh(*, force_replace=False):
        assert force_replace is True
        raise RuntimeError("Ozon HTTP session не настроена")

    monkeypatch.setattr(product_test_agent, "_execute_job", execute)
    monkeypatch.setattr(product_test_agent, "_ensure_ozon_session", cancel_refresh)
    monkeypatch.setattr(product_test_agent, "_log", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="не настроена"):
        asyncio.run(
            product_test_agent._run_job_with_retry(
                {"job_type": "validate_supplier"},
                merchant_session=object(),
                store_id="store-1",
                workspace_id=1,
            )
        )


def test_product_test_agent_dispatches_popular_discovery_without_ozon(monkeypatch) -> None:
    merchant_catalog = object()
    captured: dict = {}

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", lambda *_args, **_kwargs: merchant_catalog)

    def fake_discover(**kwargs):
        captured.update(kwargs)
        return {"mode": "popular", "rows": []}

    monkeypatch.setattr(product_test_agent, "discover_popular_products", fake_discover)
    result = asyncio.run(
        product_test_agent._execute_job(
            {
                "job_type": "discover_popular",
                "reference": "Ежовик гребенчатый",
                "city_id": "196220100",
                "zone_id": "Magnum_ZONE1",
                "options": {
                    "target_new": 10,
                    "max_kaspi_scan": 200,
                    "minimum_reviews": 50,
                    "maximum_sellers": 5,
                    "existing_kaspi_ids": ["123"],
                },
            },
            merchant_session=object(),
            store_id="store-1",
        )
    )

    assert result == {"mode": "popular", "rows": []}
    assert captured["query"] == "Ежовик гребенчатый"
    assert captured["minimum_reviews"] == 50
    assert captured["minimum_price_kzt"] == 20000
    assert captured["maximum_sellers"] == 5
    assert captured["existing_kaspi_ids"] == {"123"}
    assert captured["merchant_catalog"] is merchant_catalog


def test_initial_creation_process_includes_minimum_preorder(monkeypatch) -> None:
    api = object.__new__(MerchantOfferApi)
    api.store_id = "11843018_041600"
    api.city_id = "196220100"
    api.merchant_uid = "merchant-1"
    requests: list[dict] = []
    response = SimpleNamespace(
        is_success=True,
        status_code=200,
        text='{"id":"operation-1"}',
        json=lambda: {"id": "operation-1"},
    )
    monkeypatch.setattr(
        api,
        "_request_json",
        lambda method, url, *, json_body: requests.append({"method": method, "url": url, "json": json_body}) or response,
    )

    result = api._initial_manual_process(
        merchant_sku="138791468_857843219",
        model="GLS Omega-3",
        price=9599,
        stock=5,
        preorder=0,
    )

    assert result["ok"] is True
    assert requests[0]["json"]["availabilities"][0]["preOrder"] == 1
    preview = api.create_flow_preview(
        master_sku="138791468",
        model="GLS Omega-3",
        price=9599,
        stock=5,
        preorder=0,
    )
    initial = next(step for step in preview["steps"] if step["name"] == "initial_process")
    assert initial["json"]["availabilities"][0]["preOrder"] == 1


def test_read_offer_detects_approved_product_import_row_without_availability(monkeypatch) -> None:
    api = object.__new__(MerchantOfferApi)
    api.store_id = "11843018_041600"
    api.city_id = "196220100"
    api.merchant_uid = "merchant-1"
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "data": {
                "content": [
                    {
                        "sku": "4671307561",
                        "masterSku": "1671307561",
                        "availabilities": [],
                        "cityPrices": [],
                        "available": False,
                        "processed": True,
                        "status": "INACTIVE",
                    }
                ]
            }
        },
    )
    monkeypatch.setattr(api, "_request_json", lambda *_args, **_kwargs: response)

    state = api.read_offer("4671307561")

    assert state.found is True
    assert state.sku == "4671307561"
    assert state.master_sku == "1671307561"
    assert state.price_kzt is None
    assert state.stock_count is None
    assert state.preorder_days is None


def test_approved_product_import_row_is_filled_without_creating_duplicate(monkeypatch) -> None:
    api = object.__new__(MerchantOfferApi)
    states = iter(
        [
            OfferState(
                found=True,
                sku="4671307561",
                master_sku="1671307561",
                stock_count=None,
                preorder_days=None,
                price_kzt=None,
            ),
            OfferState(
                found=True,
                sku="4671307561",
                master_sku="1671307561",
                stock_count=5,
                preorder_days=3,
                price_kzt=12990,
            ),
        ]
    )
    writes: list[dict] = []
    monkeypatch.setattr(api, "read_offer", lambda _reference: next(states))
    monkeypatch.setattr(
        api,
        "process_offer",
        lambda **kwargs: writes.append(kwargs) or {"accepted": True, "status_code": 200},
    )

    result = api.create_linked_offer(
        master_sku="1671307561",
        model="Фертилипан 30 саше",
        price=12990,
        stock=5,
        preorder=3,
        live=True,
        attempts=1,
        poll_seconds=0.5,
    )

    assert result["result"] == "ALREADY_EXISTS"
    assert result["merchant_sku"] == "4671307561"
    assert result["after"]["price_kzt"] == 12990
    assert writes == [
        {
            "sku": "4671307561",
            "model": "Фертилипан 30 саше",
            "price": 12990,
            "stock": 5,
            "preorder": 3,
            "live": True,
        }
    ]


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


def test_product_test_agent_waits_for_new_card_master_sku(monkeypatch) -> None:
    class FakeMerchantOfferApi:
        def __init__(self, _session, *, store_id: str, city_id: str) -> None:
            pass

        def read_offer(self, reference: str) -> OfferState:
            assert reference == "900000001"
            return OfferState(found=False, sku=None)

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", FakeMerchantOfferApi)
    result = asyncio.run(product_test_agent._execute_job(
        {
            "job_type": "confirm_new_card",
            "city_id": "196220100",
            "options": {
                "official_sku": "900000001",
                "model": "New Solgar card",
                "initial_price_kzt": 5200,
                "stock_count": 5,
                "preorder_days": 1,
            },
        },
        merchant_session=object(),
        store_id="11843018_041600",
    ))

    assert result["result"] == "NEW_CARD_PENDING_MODERATION"
    assert result["official_sku"] == "900000001"


def test_product_test_agent_repairs_new_card_offer_with_minimum_preorder(monkeypatch) -> None:
    captured: dict = {}

    class FakeMerchantOfferApi:
        def __init__(self, _session, *, store_id: str, city_id: str) -> None:
            pass

        def read_offer(self, reference: str) -> OfferState:
            assert reference == "900000001"
            return OfferState(found=True, sku="900000001", master_sku="880000001")

        def create_linked_offer(self, **kwargs):
            captured.update(kwargs)
            return {
                "result": "ALREADY_EXISTS",
                "merchant_sku": "880000001_900000001",
                "after": {
                    "found": True,
                    "sku": "880000001_900000001",
                    "price_kzt": 5200,
                    "stock_count": 5,
                    "preorder_days": 1,
                },
            }

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", FakeMerchantOfferApi)
    result = asyncio.run(product_test_agent._execute_job(
        {
            "job_type": "confirm_new_card",
            "city_id": "196220100",
            "options": {
                "official_sku": "900000001",
                "model": "New Solgar card",
                "initial_price_kzt": 5200,
                "stock_count": 5,
                "preorder_days": 0,
            },
        },
        merchant_session=object(),
        store_id="11843018_041600",
    ))

    assert captured["master_sku"] == "880000001"
    assert captured["preorder"] == 1
    assert result["new_card_master_sku"] == "880000001"


def test_product_test_agent_blocks_duplicate_new_card_import(monkeypatch) -> None:
    class FakeMerchantOfferApi:
        def __init__(self, _session, *, store_id: str, city_id: str) -> None:
            pass

        def read_offer(self, reference: str) -> OfferState:
            assert reference == "900000001"
            return OfferState(
                found=True,
                sku="880000001_900000001",
                master_sku="880000001",
            )

    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", FakeMerchantOfferApi)
    monkeypatch.setattr(
        product_test_agent,
        "prepare_new_card",
        lambda _token, url: {"source_url": url, "sku": "900000001"},
    )
    monkeypatch.setattr(
        product_test_agent,
        "validate_supplier_url",
        lambda *_args, **_kwargs: pytest.fail("supplier validation must not run for a duplicate SKU"),
    )

    with pytest.raises(RuntimeError, match="уже существует"):
        asyncio.run(product_test_agent._execute_job(
            {
                "job_type": "prepare_new_card",
                "city_id": "196220100",
                "reference": "https://www.ozon.kz/product/new-card-900000001/",
                "options": {"supplier_url": "https://www.ozon.kz/product/new-card-900000001/"},
            },
            merchant_session=object(),
            store_id="11843018_041600",
            kaspi_api_token_provider=lambda: "secret-token",
        ))


def test_archive_preorder_inspection_reads_inactive_cabinet_without_public_card(monkeypatch):
    class Catalog:
        def __init__(self, *_args, **_kwargs):
            pass
        def read_offer(self, reference):
            assert reference == "117556298_386692612"
            return OfferState(found=True, sku=reference, master_sku="117556298",
                              row_available=False, query_mode="inactive", model="Ayusri")
    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", Catalog)
    async def no_public(**kwargs):
        raise AssertionError("Archive restoration must not require a public card")
    monkeypatch.setattr(product_test_agent, "inspect_kaspi_product", no_public)
    result = asyncio.run(product_test_agent._execute_job(
        {"job_type": "inspect", "reference": "117556298",
         "options": {"manual_preorder": True, "preorder_archive": True,
                     "source_sku": "117556298_386692612", "master_sku": "117556298"}},
        merchant_session=object(), store_id="store-1"))
    assert result["product_name"] == "Ayusri"
    assert result["kaspi_product_id"] == "117556298"
    assert result["catalog_state"]["query_mode"] == "inactive"


@pytest.mark.parametrize("activated", [True, False])
def test_archived_offer_with_matching_values_requires_activation(monkeypatch, activated):
    api = object.__new__(MerchantOfferApi)
    before = OfferState(found=True, sku="117556298_386692612", master_sku="117556298",
                        price_kzt=4500, stock_count=5, preorder_days=5,
                        row_available=False, query_mode="inactive")
    after = OfferState(found=True, sku=before.sku, master_sku=before.master_sku,
                       price_kzt=4500, stock_count=5, preorder_days=5,
                       row_available=activated, query_mode="active" if activated else "inactive")
    states = iter([before, after])
    references, writes = [], []
    def read(reference):
        references.append(reference)
        return next(states)
    monkeypatch.setattr(api, "read_offer", read)
    monkeypatch.setattr(api, "process_offer", lambda **kwargs: writes.append(kwargs) or {"accepted": True})
    result = api.create_linked_offer(master_sku=before.master_sku, merchant_sku=before.sku,
        model="Ayusri", price=4500, stock=5, preorder=5, live=True, attempts=1, poll_seconds=0.5)
    assert writes[0]["sku"] == before.sku
    assert references == [before.sku, before.sku]
    assert result["result"] == ("ALREADY_EXISTS" if activated else "EXISTING_PROCESS_ACCEPTED_NOT_CONFIRMED")


def test_missing_exact_archive_sku_never_creates_a_duplicate(monkeypatch):
    api = object.__new__(MerchantOfferApi)
    monkeypatch.setattr(api, "read_offer", lambda ref: OfferState(found=False, sku=ref))
    with pytest.raises(ValueError, match="новый оффер не создавался"):
        api.create_linked_offer(master_sku="117556298", merchant_sku="117556298_386692612",
            model="Ayusri", price=4500, stock=5, preorder=5, live=True)


def test_missing_merchant_sku_never_falls_back_to_public_creation(monkeypatch):
    class Catalog:
        def __init__(self, *_args, **_kwargs):
            pass
        def read_offer(self, reference):
            return OfferState(found=False, sku=reference)
    monkeypatch.setattr(product_test_agent, "MerchantOfferApi", Catalog)
    async def no_public(**kwargs):
        raise AssertionError("Missing exact merchant SKU must not create another offer")
    monkeypatch.setattr(product_test_agent, "inspect_kaspi_product", no_public)
    with pytest.raises(ValueError, match="ни в продаже, ни в архиве"):
        asyncio.run(product_test_agent._execute_job(
            {"job_type": "inspect", "options": {"manual_preorder": True,
             "source_sku": "117556298_386692612", "master_sku": "117556298"}},
            merchant_session=object(), store_id="store-1"))
