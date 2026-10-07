from decimal import Decimal
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from backend.app.preorder_api import (
    PreorderRequest,
    card_id,
    connect_preorder,
    read_preorders,
    PreorderEdit,
    edit_preorder,
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
        assert db_session.scalar(select(FastDumpingPolicy)) is None
        assert (
            product.kaspi_product_id == "123456789"
            and product.merchant_sku == "actual-merchant-sku"
        )
        item = db_session.get(ProductTestItem, queued["item"]["id"])
        assert (
            not item.active
            and not item.supplier_url
            and item.status == "preorder_connected"
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


def test_edit_reuses_product_and_waits_for_exact_kaspi_confirmation(db_session):
    from backend.app.fast_dumping_models import FastDumpingState

    with workspace_context(1):
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(
            select(ProductTestJob).where(ProductTestJob.job_type == "create_offer")
        )
        enrolled = _enroll_created_product(db_session, job=write, result=confirmed())
        product_id = enrolled["product_id"]
        state = db_session.scalar(select(FastDumpingState))
        updated = edit_preorder(
            queued["item"]["id"], PreorderEdit(price_kzt=15999, preorder_days=4),
            db_session,
        )
        assert updated["item"]["id"] == queued["item"]["id"]
        assert updated["item"]["product_id"] == product_id
        assert state is None
        read_job = db_session.get(ProductTestJob, updated["job"]["id"])
        inspect(db_session, read_job)
        next_write = db_session.scalar(
            select(ProductTestJob).where(
                ProductTestJob.job_type == "create_offer",
                ProductTestJob.status == "queued",
            )
        )
        assert next_write.options_json["initial_price_kzt"] == 15999
        assert next_write.options_json["preorder_days"] == 4
        with pytest.raises(ValueError):
            _enroll_created_product(db_session, job=next_write, result=confirmed())
        assert state is None
        done = _enroll_created_product(
            db_session, job=next_write,
            result=confirmed(price_kzt=15999, preorder_days=4),
        )
        assert done["product_id"] == product_id
        assert db_session.scalar(select(FastDumpingState)) is None
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 1


def test_edit_rejects_pending_job_and_other_workspace(db_session):
    with workspace_context(1):
        queued = queue(db_session)
        with pytest.raises(HTTPException) as exc:
            edit_preorder(
                queued["item"]["id"], PreorderEdit(price_kzt=15000, preorder_days=3),
                db_session,
            )
        assert exc.value.status_code == 409
        assert db_session.get(ProductTestItem, queued["item"]["id"]).test_price_kzt == 14000
    with workspace_context(3):
        with pytest.raises(HTTPException) as exc:
            edit_preorder(
                queued["item"]["id"], PreorderEdit(price_kzt=15000, preorder_days=3),
                db_session,
            )
        assert exc.value.status_code == 404


def test_edit_cannot_modify_ordinary_product_test_item(db_session):
    with workspace_context(1):
        item = ProductTestItem(
            workspace_id=1, input_reference=URL, kaspi_product_id="123456789",
            merchant_sku="ordinary-test", name="Ordinary test", kaspi_url=URL,
        )
        db_session.add(item)
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            edit_preorder(item.id, PreorderEdit(price_kzt=15000, preorder_days=3), db_session)
        assert exc.value.status_code == 404


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


def test_physical_stock_blocks_but_enabled_settings_do_not_block_preorder(db_session):
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        product.kaspi_product_id = "123456789"
        db_session.commit()
        with pytest.raises(HTTPException, match="409"):
            queue(db_session)
        batch.quantity_remaining = 0
        db_session.commit()
        queue(db_session)
        assert policy.enabled
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
                version="1.1.25",
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
        assert db_session.get(FastDumpingPolicy, policy_id) is policy
        assert not policy.enabled
        assert product.sale_enabled and not product.sale_state_overridden
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1


@pytest.mark.parametrize("reference", ["123456789", "123456789_old-store"])
def test_master_sku_reference_uses_existing_inspector(db_session, reference):
    with workspace_context(1):
        queued = connect_preorder(PreorderRequest(
            reference=reference, price_kzt=14000, preorder_days=7, stock_count=12,
        ), db_session)
        assert queued["job"]["reference"] == "123456789"
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer"))
        assert write.options_json["stock_count"] == 12
        with pytest.raises(ValueError):
            _enroll_created_product(db_session, job=write, result=confirmed())
        done = _enroll_created_product(db_session, job=write, result=confirmed(stock_count=12))
        assert done["item"]["stock_count"] == 12
        edited = edit_preorder(queued["item"]["id"],
            PreorderEdit(price_kzt=15000, preorder_days=8), db_session)
        assert edited["item"]["stock_count"] == 12  # Older clients preserve quantity.
        inspect(db_session, db_session.get(ProductTestJob, edited["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer", ProductTestJob.status == "queued"))
        _enroll_created_product(db_session, job=write,
            result=confirmed(price_kzt=15000, preorder_days=8, stock_count=12))
        edited = edit_preorder(queued["item"]["id"],
            PreorderEdit(price_kzt=15000, preorder_days=8, stock_count=3), db_session)
        inspect(db_session, db_session.get(ProductTestJob, edited["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer", ProductTestJob.status == "queued"))
        done = _enroll_created_product(db_session, job=write,
            result=confirmed(price_kzt=15000, preorder_days=8, stock_count=3))
        assert done["item"]["stock_count"] == 3
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1


def test_old_merchant_sku_adopts_existing_test_preorder_without_duplicates(db_session):
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        product.merchant_sku = "old-sku"
        policy.enabled = False
        item = ProductTestItem(workspace_id=1, input_reference=URL,
            kaspi_product_id=product.kaspi_product_id, merchant_sku="old-sku",
            name="Old phone", kaspi_url=URL, product_id=product.id,
            stock_count=5, preorder_days=7, active=True,
            offers_json={"supplier": {"cost_kzt": 9000}})
        db_session.add(item)
        db_session.commit()
        queued = connect_preorder(PreorderRequest(reference="old-sku",
            price_kzt=14000, preorder_days=7, stock_count=9), db_session)
        assert queued["item"]["id"] == item.id
        assert queued["item"]["product_id"] == product.id
        assert not item.active
        assert item.offers_json["supplier"]["cost_kzt"] == 9000
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer"))
        result = confirmed(sku="old-sku", stock_count=9)
        result["merchant_sku"] = "old-sku"
        done = _enroll_created_product(db_session, job=write, result=result)
        assert done["product_id"] == product.id
        assert read_preorders(db_session)["items"][0]["id"] == item.id
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 1
    with workspace_context(3):
        queued = connect_preorder(PreorderRequest(reference="old-sku",
            price_kzt=14000, preorder_days=7), db_session)
        assert queued["item"]["product_id"] is None
        assert queued["item"]["kaspi_product_id"].startswith("sku:")
        assert db_session.get(ProductTestJob, queued["job"]["id"]).options_json["master_sku"] is None


def test_adopting_existing_test_item_waits_for_its_pending_job(db_session):
    with workspace_context(1):
        item = ProductTestItem(workspace_id=1, input_reference=URL,
            kaspi_product_id="123456789", merchant_sku="old-sku",
            name="Phone", kaspi_url=URL, active=True)
        db_session.add(item)
        db_session.flush()
        db_session.add(ProductTestJob(workspace_id=1, item_id=item.id,
            input_reference=URL, job_type="inspect", status="leased"))
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            connect_preorder(PreorderRequest(reference="old-sku",
                price_kzt=14000, preorder_days=7), db_session)
        assert exc.value.status_code == 409
        assert item.active and item.input_reference == URL


@pytest.mark.parametrize("quantity", [0, -1, 1000001, 1.5])
def test_invalid_preorder_quantity_is_rejected(quantity):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PreorderRequest(reference=URL, price_kzt=14000, preorder_days=7, stock_count=quantity)
    with pytest.raises(ValidationError):
        PreorderEdit(price_kzt=14000, preorder_days=7, stock_count=quantity)


def test_archived_sku_imported_as_product_id_is_normalized_without_losing_history(db_session):
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "117556298_386692612"
        product.merchant_sku = "117556298_386692612"
        product.sale_enabled = False
        policy.enabled = False
        db_session.commit()
        saved_id = product.id
        queued = connect_preorder(PreorderRequest(reference=product.merchant_sku,
            price_kzt=4500, preorder_days=5, stock_count=5), db_session)
        assert db_session.get(ProductTestJob, queued["job"]["id"]).options_json["master_sku"] == "117556298"
        assert queued["item"]["product_id"] == saved_id
        _persist_product_inspection(db_session,
            job=db_session.get(ProductTestJob, queued["job"]["id"]),
            result={"kaspi_product_id": "117556298", "product_name": "Ayusri",
                    "catalog_state": {"found": True, "sku": product.merchant_sku,
                                      "master_sku": "117556298"}})
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        assert write.options_json["merchant_sku"] == "117556298_386692612"
        state = {"found": True, "sku": "117556298_386692612", "master_sku": "117556298",
                 "stock_count": 5, "preorder_days": 5, "price_kzt": 4500,
                 "row_available": False, "query_mode": "inactive"}
        result = {"after": state, "merchant_sku": state["sku"], "master_sku": state["master_sku"]}
        with pytest.raises(ValueError):
            _enroll_created_product(db_session, job=write, result=result)
        assert not product.sale_enabled and product.kaspi_product_id == "117556298_386692612"
        state.update(row_available=True, query_mode="active", nested_available="yes")
        done = _enroll_created_product(db_session, job=write, result=result)
        assert done["product_id"] == saved_id
        assert product.kaspi_product_id == "117556298"
        assert product.merchant_sku == "117556298_386692612"
        assert product.sale_enabled
        assert db_session.scalar(select(func.count()).select_from(Product)) == 1


def test_unknown_archive_sku_resolves_to_existing_preorder_record(db_session):
    with workspace_context(1):
        earlier = queue(db_session)
        earlier_job = db_session.get(ProductTestJob, earlier["job"]["id"])
        earlier_job.status = "failed"
        db_session.commit()
        queued = connect_preorder(PreorderRequest(reference="archive-only-sku",
            price_kzt=14000, preorder_days=7, stock_count=9), db_session)
        resolving = db_session.get(ProductTestJob, queued["job"]["id"])
        assert queued["item"]["kaspi_product_id"].startswith("sku:")
        with pytest.raises(ValueError):
            inspect(db_session, resolving)  # Unmapped SKU needs cabinet proof.
        result = _persist_product_inspection(db_session, job=resolving,
            result={"kaspi_product_id": "123456789", "product_name": "Phone",
                    "catalog_state": {"found": True, "sku": "archive-only-sku",
                                      "master_sku": "123456789"}})
        assert result["item"]["id"] == earlier["item"]["id"]
        assert resolving.item_id == earlier_job.item_id
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 1
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        state = confirmed(stock_count=9, sku="archive-only-sku")
        state["merchant_sku"] = "archive-only-sku"
        done = _enroll_created_product(db_session, job=write, result=state)
        assert done["item"]["id"] == earlier["item"]["id"]


def test_old_agent_cannot_claim_archive_preorder_but_new_agent_can(db_session, monkeypatch):
    from backend.app import product_test_api as api
    monkeypatch.setattr(api, "_validate_workspace_merchant", lambda *a, **k: None)
    monkeypatch.setattr(api, "_touch_product_test_agent", lambda *a, **k: None)
    with workspace_context(1):
        queued = queue(db_session)
        identity = dict(agent_id="archive-test", workspace_id=1, merchant_uid="merchant", agent_kind="product_test")
        old = api.claim_product_test_job(api.ProductTestAgentIdentity(**identity, version="1.1.24"), db_session)
        assert old["job"] is None
        assert db_session.get(ProductTestJob, queued["job"]["id"]).status == "queued"
        new = api.claim_product_test_job(api.ProductTestAgentIdentity(**identity, version="1.1.25"), db_session)
        assert new["job"]["id"] == queued["job"]["id"]
        assert new["job"]["options"]["preorder_archive"] is True


def test_numeric_merchant_sku_is_not_confused_with_master_id(db_session):
    with workspace_context(1):
        queued = connect_preorder(PreorderRequest(reference="4671307561",
            price_kzt=14000, preorder_days=7), db_session)
        _persist_product_inspection(db_session,
            job=db_session.get(ProductTestJob, queued["job"]["id"]),
            result={"kaspi_product_id": "1671307561", "product_name": "Phone",
                    "catalog_state": {"found": True, "sku": "4671307561", "master_sku": "1671307561"}})
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        assert write.options_json["master_sku"] == "1671307561"
        assert write.options_json["merchant_sku"] == "4671307561"


def _snapshot(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


@pytest.mark.parametrize("pricing_mode", ["manual", "automation"])
def test_preorder_connect_and_edit_leave_pricing_policies_state_and_jobs_unchanged(db_session, pricing_mode):
    from backend.app.dumping_models import DumpingPolicy, DumpingRun
    from backend.app.fast_dumping_models import FastDumpingJob
    product, batch, fast, fast_state = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        product.merchant_sku = "actual-merchant-sku"
        fast.pricing_mode = pricing_mode
        fast_state.own_price_kzt = 12345
        classic = DumpingPolicy(product_id=product.id, enabled=True,
            auto_publish_xml=True, minimum_profit_kzt=3456, undercut_step_kzt=7,
            supplier_delivery_buffer_days=3, city_id="196220100", zone_id="Magnum_ZONE1")
        db_session.add(classic)
        db_session.commit()
        for row in (fast, fast_state, classic):
            db_session.refresh(row)
        before = [_snapshot(row) for row in (fast, fast_state, classic)]
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        done = _enroll_created_product(db_session, job=write, result=confirmed())
        assert done["product_id"] == product.id
        edited = edit_preorder(queued["item"]["id"],
            PreorderEdit(price_kzt=15999, preorder_days=4, stock_count=10), db_session)
        inspect(db_session, db_session.get(ProductTestJob, edited["job"]["id"]))
        next_write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer", ProductTestJob.status == "queued"))
        _enroll_created_product(db_session, job=next_write,
            result=confirmed(price_kzt=15999, preorder_days=4, stock_count=10))
        for row in (fast, fast_state, classic):
            db_session.refresh(row)
        assert [_snapshot(row) for row in (fast, fast_state, classic)] == before
        assert db_session.scalar(select(func.count()).select_from(FastDumpingJob)) == 0
        assert db_session.scalar(select(func.count()).select_from(DumpingRun)) == 0


@pytest.mark.parametrize("status", ["queued_apply", "leased_apply", "queued_verify", "leased_verify"])
def test_only_outstanding_same_offer_write_blocks_preorder_without_changing_it(db_session, status):
    from backend.app.fast_dumping_models import FastDumpingJob
    product, batch, fast, state = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        job = FastDumpingJob(workspace_id=1, product_id=product.id,
            policy_id=fast.id, status=status)
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        before = _snapshot(job)
        with pytest.raises(HTTPException) as exc:
            queue(db_session)
        assert exc.value.status_code == 409
        assert "предыдущую запись" in exc.value.detail
        db_session.refresh(job)
        assert _snapshot(job) == before
        assert db_session.scalar(select(func.count()).select_from(ProductTestJob)) == 0
