from decimal import Decimal
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from backend.app.preorder_api import (
    PreorderRequest,
    card_id,
    connect_preorder,
    read_preorders,
)
from backend.app.product_test_api import (
    _persist_product_inspection,
    _enroll_created_product,
)
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app.fast_dumping_models import FastDumpingPolicy
from backend.app.fast_dumping_api import (
    upsert_fast_dumping_policy,
    FastDumpingPolicyUpsert,
)
from backend.app.full_automation_api import save_automation, AutomationSettings
from backend.app.dumping_models import KaspiXmlFeed
from backend.app.models import Product
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _seed_fast_product

URL = "https://kaspi.kz/shop/p/phone-123456789/"


def queue(db):
    return connect_preorder(
        PreorderRequest(reference=URL, price_kzt=14000, preorder_days=7), db
    )


def inspect(db, job):
    return _persist_product_inspection(
        db,
        job=job,
        result={
            "kaspi_product_id": "123456789",
            "product_name": "Phone",
            "brand": "Brand",
            "product_url": URL,
            "image_url": "https://resources.cdn-kaspi.kz/img/m/p/test.jpg",
        },
    )


def confirmed(**changes):
    state = {
        "found": True,
        "sku": "actual-merchant-sku",
        "master_sku": "123456789",
        "price_kzt": 14000,
        "stock_count": 5,
        "preorder_days": 7,
    }
    state.update(changes)
    return {
        "merchant_sku": "actual-merchant-sku",
        "master_sku": "123456789",
        "after": state,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://kaspi.kz/shop/p/x-123456789/",
        "https://example.org/shop/p/x-123456789/",
        "https://kaspi.kz.evil.org/shop/p/x-123456789/",
        "https://user:secret@kaspi.kz/shop/p/x-123456789/",
        "https://kaspi.kz:evil/shop/p/x-123456789/",
        "https://kaspi.kz/shop/c/phone/",
    ],
)
def test_reference_rejects_non_card_urls(url):
    with pytest.raises(HTTPException) as exc:
        card_id(url)
    assert exc.value.status_code == 422


def test_existing_agent_pipeline_enrolls_with_requested_price_and_no_supplier(
    db_session,
):
    with workspace_context(1):
        feed = KaspiXmlFeed(
            workspace_id=1,
            source_xml='<kaspi_catalog><company>LEO</company><merchantid>1</merchantid><offers><offer sku="other"><model>Other</model><availabilities><availability available="yes" storeId="PP1"/></availabilities><cityprices><cityprice cityId="196220100">20000</cityprice></cityprices></offer></offers></kaspi_catalog>',
            generated_xml="",
        )
        db_session.add(feed)
        queued = queue(db_session)
        read_job = db_session.get(ProductTestJob, queued["job"]["id"])
        assert read_job.job_type == "inspect"
        inspect(db_session, read_job)
        write_job = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        assert write_job.options_json["initial_price_kzt"] == 14000
        assert write_job.options_json["preorder_days"] == 7
        assert write_job.options_json["stock_count"] == 5
        done = _enroll_created_product(db_session, job=write_job, result=confirmed())
        product = db_session.get(Product, done["product_id"])
        policy = db_session.get(FastDumpingPolicy, done["fast_dumping_policy_id"])
        assert (
            product.kaspi_product_id == "123456789"
            and product.merchant_sku == "actual-merchant-sku"
        )
        assert not policy.enabled
        item = db_session.get(ProductTestItem, queued["item"]["id"])
        assert (
            not item.active
            and not item.supplier_url
            and item.status == "enrolled_fast_dumping"
        )
        assert (
            "actual-merchant-sku" in feed.generated_xml
            and "actual-merchant-sku" in feed.source_xml
        )
        assert (
            "other" in feed.generated_xml
            and "14000" in feed.generated_xml
            and 'preOrder="7"' in feed.generated_xml
        )
        assert read_preorders(db_session)["items"][0]["product_id"] == product.id
        queue(db_session)
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 1
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1


def test_duplicate_pending_preorder_is_rejected(db_session):
    with workspace_context(1):
        queue(db_session)
        with pytest.raises(HTTPException) as exc:
            queue(db_session)
        assert exc.value.status_code == 409
        assert db_session.scalar(select(func.count()).select_from(ProductTestJob)) == 1


@pytest.mark.parametrize(
    "changes",
    [{"price_kzt": 13999}, {"stock_count": 4}, {"preorder_days": 6}, {"found": False}],
)
def test_unconfirmed_offer_cannot_create_crm_product(db_session, changes):
    with workspace_context(1):
        q = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, q["job"]["id"]))
        job = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        with pytest.raises(ValueError):
            _enroll_created_product(db_session, job=job, result=confirmed(**changes))
        assert db_session.scalar(select(func.count()).select_from(Product)) == 0


def test_wrong_inspection_card_cannot_queue_a_write(db_session):
    with workspace_context(1):
        q = queue(db_session)
        with pytest.raises(ValueError):
            _persist_product_inspection(
                db_session,
                job=db_session.get(ProductTestJob, q["job"]["id"]),
                result={"kaspi_product_id": "99999999", "product_name": "Wrong"},
            )
        assert db_session.scalar(select(func.count()).select_from(ProductTestJob)) == 1


def test_physical_stock_and_enabled_fast_block_preorder(db_session):
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        product.kaspi_product_id = "123456789"
        db_session.commit()
        with pytest.raises(HTTPException, match="409"):
            queue(db_session)
        batch.quantity_remaining = 0
        db_session.commit()
        with pytest.raises(HTTPException, match="409"):
            queue(db_session)
        policy.enabled = False
        db_session.commit()
        queue(db_session)
        for call in (
            lambda: upsert_fast_dumping_policy(
                product.id, FastDumpingPolicyUpsert(), db_session
            ),
            lambda: save_automation(product.id, AutomationSettings(), db_session),
        ):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 409


def test_preorder_history_is_workspace_scoped(db_session):
    with workspace_context(1):
        queue(db_session)
        assert len(read_preorders(db_session)["items"]) == 1
    with workspace_context(3):
        assert read_preorders(db_session)["items"] == []
        queue(db_session)
        assert len(read_preorders(db_session)["items"]) == 1


def test_stock_arriving_while_waiting_prevents_agent_write(db_session, monkeypatch):
    from backend.app import product_test_api as api

    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        policy.enabled = False
        db_session.commit()
        q = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, q["job"]["id"]))
        batch.quantity_remaining = 4
        db_session.commit()
        monkeypatch.setattr(api, "_validate_workspace_merchant", lambda *a, **k: None)
        monkeypatch.setattr(api, "_touch_product_test_agent", lambda *a, **k: None)
        result = api.claim_product_test_job(
            api.ProductTestAgentIdentity(
                agent_id="preorder-test",
                workspace_id=1,
                merchant_uid="merchant",
                agent_kind="product_test",
            ),
            db_session,
        )
        assert result["job"] is None
        job = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        assert job.status == "failed" and job.error_code == "preorder_conflict"
        assert "физический остаток" in job.error_message


def test_invalid_xml_does_not_partly_enroll_product(db_session):
    with workspace_context(1):
        q = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, q["job"]["id"]))
        db_session.add(
            KaspiXmlFeed(
                workspace_id=1, source_xml="<invalid", generated_xml="<invalid"
            )
        )
        db_session.commit()
        job = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        with pytest.raises(ValueError, match="XML"):
            _enroll_created_product(db_session, job=job, result=confirmed())
        assert db_session.scalar(select(func.count()).select_from(Product)) == 0
        assert (
            db_session.get(ProductTestItem, q["item"]["id"]).merchant_sku
            == "manual-preorder:123456789"
        )


def test_resurrection_reuses_product_and_preserves_manual_preorder(db_session):
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        product.merchant_sku = "actual-merchant-sku"
        product.sale_enabled = False
        product.sale_state_overridden = True
        policy.enabled = False
        db_session.commit()
        product_id, policy_id = product.id, policy.id
        q = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, q["job"]["id"]))
        job = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        done = _enroll_created_product(db_session, job=job, result=confirmed())
        assert done["product_id"] == product_id
        assert done["fast_dumping_policy_id"] == policy_id
        assert product.sale_enabled and not product.sale_state_overridden
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1
