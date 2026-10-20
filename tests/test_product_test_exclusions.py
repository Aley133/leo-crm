import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app import product_test_api as api
from backend.app.product_test_models import ProductTestExclusion, ProductTestItem, ProductTestJob
from backend.app.workspace_context import workspace_context
from tools.product_discovery import runtime


def candidate(db, sku, brand="Solgar", workspace=1, **kwargs):
    item = ProductTestItem(workspace_id=workspace, input_reference=sku, kaspi_product_id=sku,
        merchant_sku=sku, name=f"{brand} {sku}", brand=brand,
        kaspi_url=f"https://kaspi.kz/shop/p/{sku}/", offers_json={}, **kwargs)
    with workspace_context(workspace):
        db.add(item)
        db.commit()
    return item


def test_bulk_hide_survives_next_search_and_can_be_restored(db_session, monkeypatch):
    monkeypatch.setattr(api, "_product_test_agent_status", lambda *_: {})
    a = candidate(db_session, "101")
    b = candidate(db_session, "102")
    other = candidate(db_session, "103", "Other")
    payload = api.ProductTestExcludeRequest(item_ids=[a.id, b.id, a.id])
    api.exclude_product_test_candidates(payload, db_session)
    api.exclude_product_test_candidates(payload, db_session)
    assert len(list(db_session.scalars(select(ProductTestExclusion)))) == 2
    assert [row["id"] for row in api.read_product_test_state(db_session)["items"]] == [other.id]
    queued = api.discover_product_candidates(api.ProductDiscoveryRequest(query="supplements"), db_session)
    job = db_session.get(ProductTestJob, queued["job"]["id"])
    assert set(job.options_json["existing_kaspi_ids"]) >= {"101", "102"}
    exclusions = api.read_product_test_state(db_session)["exclusions"]
    api.restore_product_test_exclusion(next(row["id"] for row in exclusions if row["value"] == "101"), db_session)
    # Other hidden product remains excluded; restore does not revive old search batches.
    assert a.active is False
    job.status = "succeeded"
    db_session.commit()
    queued = api.discover_product_candidates(api.ProductDiscoveryRequest(query="supplements"), db_session)
    ids = db_session.get(ProductTestJob, queued["job"]["id"]).options_json["existing_kaspi_ids"]
    assert "101" not in ids and "102" in ids


def test_brand_blocks_current_and_future_rows_including_old_agent_results(db_session, monkeypatch):
    monkeypatch.setattr(api, "_product_test_agent_status", lambda *_: {})
    a = candidate(db_session, "101", "SOLGAR")
    b = candidate(db_session, "102", "Solgar Plus")
    job = ProductTestJob(workspace_id=1, job_type="discover", input_reference="vitamins", options_json={})
    db_session.add(job)
    db_session.commit()
    api.exclude_product_test_candidates(api.ProductTestExcludeRequest(brand="  Solgar  "), db_session)
    api.exclude_product_test_candidates(api.ProductTestExcludeRequest(brand="SOLGAR"), db_session)
    assert len(list(db_session.scalars(select(ProductTestExclusion)))) == 1
    assert [row["id"] for row in api.read_product_test_state(db_session)["items"]] == [b.id]
    # A search was already running when the brand was blocked.
    result = api._persist_discovery(db_session, job=job, result={"rows":[
        {"kaspi_product_id":"201", "brand":"solgar", "product_name":"Hidden", "product_url":"https://kaspi.kz/shop/p/201/"},
        {"kaspi_product_id":"202", "brand":"Other", "product_name":"Visible", "product_url":"https://kaspi.kz/shop/p/202/"},
    ]})
    assert [row["kaspi_product_id"] for row in result["items"]] == ["202"]
    queued = api.discover_product_candidates(api.ProductDiscoveryRequest(query="vitamins"), db_session)
    assert db_session.get(ProductTestJob, queued["job"]["id"]).options_json["excluded_brands"] == ["solgar"]
    row = db_session.scalar(select(ProductTestExclusion))
    api.restore_product_test_exclusion(row.id, db_session)
    # Hidden rows were preserved, not deleted or detached from supplier data.
    assert db_session.get(ProductTestItem, a.id).brand == "SOLGAR"


def test_product_exclusion_also_guards_in_flight_results(db_session):
    a = candidate(db_session, "101")
    api.exclude_product_test_candidates(api.ProductTestExcludeRequest(item_ids=[a.id]), db_session)
    job = ProductTestJob(workspace_id=1, job_type="discover", input_reference="vitamins", options_json={})
    db_session.add(job)
    db_session.commit()
    result = api._persist_discovery(db_session, job=job, result={"rows":[{
        "kaspi_product_id":"101", "product_name":"Changed title", "product_url":a.kaspi_url}]})
    assert result["items"] == []
    assert a.name == "Solgar 101"


def test_exclusions_are_workspace_scoped_and_reject_publications(db_session, monkeypatch):
    monkeypatch.setattr(api, "_product_test_agent_status", lambda *_: {})
    own = candidate(db_session, "101")
    foreign = candidate(db_session, "101", workspace=2)
    waiting = candidate(db_session, "102")
    waiting.offers_json = {"kaspi_submission":{"status":"waiting"}}
    db_session.commit()
    for ids, code in [([foreign.id], 404), ([own.id, waiting.id], 409)]:
        with pytest.raises(HTTPException) as exc:
            api.exclude_product_test_candidates(api.ProductTestExcludeRequest(item_ids=ids), db_session)
        assert exc.value.status_code == code
    assert list(db_session.scalars(select(ProductTestExclusion))) == []
    api.exclude_product_test_candidates(api.ProductTestExcludeRequest(brand="Solgar"), db_session)
    exclusion = db_session.scalar(select(ProductTestExclusion))
    with workspace_context(2):
        state = api.read_product_test_state(db_session)
        assert [row["id"] for row in state["items"]] == [foreign.id]
        assert state["exclusions"] == []
        with pytest.raises(HTTPException) as exc:
            api.restore_product_test_exclusion(exclusion.id, db_session)
        assert exc.value.status_code == 404


def test_popular_scan_skips_hidden_products_and_brands_until_target(monkeypatch):
    products = [dict(master_sku=str(i), brand=brand, title=brand,
        kaspi_url=f"https://kaspi.kz/shop/p/{i}/", price_kzt=30000, reviews=100, rating=5, seller_count=2)
        for i, brand in [(1,"SOLGAR"), (2,"Other"), (3,"Solgar Plus"), (4,"Allowed")]]
    class Search:
        def __init__(self, *_): pass
        def close(self): pass
        def search_until(self, *args, **kwargs):
            assert kwargs["stop_after_page"](products[:2]) is False
            assert kwargs["stop_after_page"](products[2:]) is True
            return {"products":products, "stats":{"stop_reason":"target_reached"}}
    checked = []
    def count(product, *_):
        checked.append(product["master_sku"])
        return 2, {}
    monkeypatch.setattr(runtime, "KaspiProductSearch", Search)
    result = runtime.discover_popular_products(query="vitamins", city_id="196220100", zone_id="Magnum_ZONE1",
        target_new=2, max_kaspi_scan=200, excluded_brands={"  solgar "}, existing_kaspi_ids={"2"},
        seller_count_resolver=count)
    assert {row["kaspi_product_id"] for row in result["rows"]} == {"3", "4"}
    assert set(checked) == {"3", "4"}
