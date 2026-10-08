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


@pytest.mark.parametrize("duplicate_sku", ["another-offer", "actual-merchant-sku"])
@pytest.mark.parametrize("duplicate_master", ["123456789", "123456789_legacy"])
def test_edit_targets_linked_product_even_with_same_master_history(db_session, monkeypatch, duplicate_sku, duplicate_master):
    with workspace_context(1):
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        enrolled = _enroll_created_product(db_session, job=write, result=confirmed())
        duplicate = Product(workspace_id=1, name="Historical row",
                            kaspi_product_id=duplicate_master, merchant_sku=duplicate_sku)
        other_item = ProductTestItem(workspace_id=1, input_reference=URL,
            kaspi_product_id="123456789", merchant_sku="another-test-offer",
            name="Other offer", kaspi_url=URL, product_id=duplicate.id)
        db_session.add_all([duplicate, other_item])
        db_session.commit()
        original_id = enrolled["product_id"]
        edited = edit_preorder(queued["item"]["id"],
            PreorderEdit(price_kzt=15999, preorder_days=4), db_session)
        read_job = db_session.get(ProductTestJob, edited["job"]["id"])
        assert read_job.options_json["source_sku"] == "actual-merchant-sku"
        assert edited["item"]["product_id"] == original_id
        inspect(db_session, read_job)
        write = db_session.scalar(select(ProductTestJob).where(
            ProductTestJob.job_type == "create_offer", ProductTestJob.status == "queued"))
        assert write.options_json["merchant_sku"] == "actual-merchant-sku"
        from backend.app import product_test_api as api
        monkeypatch.setattr(api, "_validate_workspace_merchant", lambda *a, **k: None)
        monkeypatch.setattr(api, "_touch_product_test_agent", lambda *a, **k: None)
        claimed = api.claim_product_test_job(api.ProductTestAgentIdentity(
            agent_id="preorder-test", version="1.1.25", workspace_id=1,
            merchant_uid="merchant", agent_kind="product_test"), db_session)
        assert claimed["job"]["id"] == write.id
        done = _enroll_created_product(db_session, job=write,
            result=confirmed(price_kzt=15999, preorder_days=4))
        assert done["product_id"] == original_id
        assert done["item"]["id"] == queued["item"]["id"]
        assert duplicate.name == "Historical row"
        assert db_session.scalar(select(func.count()).select_from(Product)) == 2
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 2


def test_exact_sku_selects_one_offer_of_master_and_preserves_other_item(db_session):
    with workspace_context(1):
        selected = Product(workspace_id=1, name="Selected", kaspi_product_id="123456789", merchant_sku="sku-selected")
        other = Product(workspace_id=1, name="Other", kaspi_product_id="123456789_old", merchant_sku="sku-other")
        db_session.add_all([selected, other])
        db_session.flush()
        selected_item = ProductTestItem(workspace_id=1, input_reference=URL,
            kaspi_product_id="123456789", merchant_sku="sku-selected", name="Selected",
            kaspi_url=URL, product_id=selected.id)
        other_item = ProductTestItem(workspace_id=1, input_reference=URL,
            kaspi_product_id="123456789", merchant_sku="sku-other", name="Other", kaspi_url=URL)
        db_session.add_all([selected_item, other_item])
        db_session.commit()
        result = connect_preorder(PreorderRequest(reference="sku-selected", price_kzt=14000, preorder_days=7), db_session)
        assert result["item"]["id"] == selected_item.id
        assert result["item"]["product_id"] == selected.id
        assert other_item.input_reference == URL


def test_master_duplicates_are_resolved_by_confirmed_cabinet_sku_before_write(db_session):
    with workspace_context(1):
        selected = Product(workspace_id=1, name="Selected", kaspi_product_id="123456789", merchant_sku="actual-merchant-sku")
        other = Product(workspace_id=1, name="Other", kaspi_product_id="123456789_old", merchant_sku="sku-other")
        db_session.add_all([selected, other])
        db_session.commit()
        queued = queue(db_session)
        assert queued["item"]["product_id"] is None
        _persist_product_inspection(db_session, job=db_session.get(ProductTestJob, queued["job"]["id"]),
            result={"kaspi_product_id": "123456789", "product_name": "Phone",
                    "catalog_state": {"found": True, "sku": "actual-merchant-sku", "master_sku": "123456789"}})
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        done = _enroll_created_product(db_session, job=write, result=confirmed())
        assert done["product_id"] == selected.id
        assert db_session.scalar(select(func.count()).select_from(Product)) == 2


def test_preorder_inspection_cannot_switch_confirmed_offer(db_session):
    with workspace_context(1):
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        _enroll_created_product(db_session, job=write, result=confirmed())
        edited = edit_preorder(queued["item"]["id"], PreorderEdit(price_kzt=15999, preorder_days=4), db_session)
        with pytest.raises(ValueError, match="точный офер"):
            _persist_product_inspection(db_session, job=db_session.get(ProductTestJob, edited["job"]["id"]),
                result={"kaspi_product_id": "123456789", "product_name": "Wrong offer",
                        "catalog_state": {"found": True, "sku": "other-sku", "master_sku": "123456789"}})


def test_exact_sku_of_another_card_is_rejected(db_session):
    from backend.app.preorder_api import _check_existing
    with workspace_context(1):
        db_session.add(Product(workspace_id=1, name="Other card", kaspi_product_id="999999999", merchant_sku="actual-merchant-sku"))
        db_session.commit()
        with pytest.raises(HTTPException, match="409"):
            _check_existing(db_session, workspace=1, kaspi_id="123456789", merchant_sku="actual-merchant-sku")


def test_exact_sku_lookup_never_uses_other_workspace_with_unscoped_session(db_session):
    from backend.app.preorder_api import _check_existing
    db_session.info["include_all_workspaces"] = True
    with workspace_context(1):
        db_session.add(Product(workspace_id=1, name="Other shop", kaspi_product_id="123456789", merchant_sku="actual-merchant-sku"))
        db_session.commit()
    with workspace_context(3):
        assert _check_existing(db_session, workspace=3, kaspi_id="123456789", merchant_sku="actual-merchant-sku") is None
    db_session.info.pop("include_all_workspaces")


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
        assert not policy.enabled
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
def test_preorder_connect_and_edit_pause_only_same_product_pricing(db_session, pricing_mode):
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
        assert not fast.enabled and not classic.enabled
        assert fast.pricing_mode == pricing_mode
        assert fast.minimum_profit_kzt == before[0]["minimum_profit_kzt"]
        assert fast_state.own_price_kzt == before[1]["own_price_kzt"]
        assert classic.minimum_profit_kzt == before[2]["minimum_profit_kzt"]
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


@pytest.mark.parametrize("rocket", [False, True])
def test_opt_in_uses_existing_monitoring_and_fast_only_after_confirmation(db_session, rocket):
    from backend.app.suppliers import ProductBinding
    from backend.app.monitoring import MonitorTarget, SupplierOfferState
    from backend.app.fast_dumping_models import FastDumpingJob
    with workspace_context(1):
        queued = connect_preorder(PreorderRequest(reference=URL, price_kzt=14000,
            preorder_days=7, stock_count=11, dump_enabled=True,
            ozon_url="https://www.ozon.kz/product/example-123456789/", rocket_enabled=rocket), db_session)
        assert db_session.scalar(select(FastDumpingPolicy)) is None
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        done = _enroll_created_product(db_session, job=write, result=confirmed(stock_count=11))
        product = db_session.get(Product, done["product_id"])
        item = db_session.get(ProductTestItem, done["item"]["id"])
        policy = db_session.scalar(select(FastDumpingPolicy))
        assert policy.enabled
        assert policy.pricing_mode == ("automation" if rocket else "manual")
        assert db_session.scalar(select(MonitorTarget)).status == "active"
        assert db_session.scalar(select(ProductBinding)).id == item.offers_json["monitor_binding_id"]
        assert db_session.scalar(select(SupplierOfferState)) is None  # never fabricate a supplier price
        assert db_session.scalar(select(FastDumpingJob)).reason == "preorder_monitoring_enabled"
        from backend.app.preorder_modes import monitored_preorder_source
        assert monitored_preorder_source(db_session, product, object()) is None


def test_monitoring_snapshot_freshness_and_fifo_priority(db_session):
    from datetime import timedelta
    from backend.app.monitoring import SupplierOfferState
    from backend.app.suppliers import ProductBinding
    from backend.app.preorder_modes import activate_preorder_pricing, monitored_preorder_source, stock_arrived, pricing_locked
    from backend.app.fast_dumping_service import utcnow
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        queued = queue(db_session)
        item = db_session.get(ProductTestItem, queued["item"]["id"])
        assert pricing_locked(db_session, product)
        item.offers_json = {**item.offers_json, "dump_enabled": True,
            "ozon_url": "https://www.ozon.kz/product/example-123456789/"}
        activate_preorder_pricing(db_session, product, item)
        binding = db_session.scalar(select(ProductBinding))
        state = SupplierOfferState(workspace_id=1, supplier_product_id=binding.supplier_product_id,
            price=5000, currency="KZT", available=True, delivery_days=6,
            fingerprint="test", adapter_schema_version="test", observed_at=utcnow(), last_checked_at=utcnow())
        db_session.add(state)
        db_session.flush()
        assert monitored_preorder_source(db_session, product, None).delivery_days == 6
        state.last_checked_at = utcnow() - timedelta(hours=1)
        assert monitored_preorder_source(db_session, product, None) is None
        state.last_checked_at = utcnow()
        state.available = False
        assert monitored_preorder_source(db_session, product, None) is None
        batch.quantity_remaining = 3
        assert stock_arrived(db_session, product.id)
        assert item.offers_json["stock_mode"] and item.status == "stock_trading"
        assert not pricing_locked(db_session, product)
        assert policy.enabled


@pytest.mark.parametrize("kwargs", [dict(dump_enabled=True), dict(rocket_enabled=True),
    dict(dump_enabled=True, ozon_url="https://example.org/product/123"),
    dict(dump_enabled=True, ozon_url="http://ozon.kz/product/123"),
    dict(dump_enabled=True, ozon_url="https://ozon.kz.evil.org/product/123")])
def test_preorder_rejects_unusable_pricing_source(kwargs):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PreorderRequest(reference=URL, price_kzt=14000, preorder_days=7, **kwargs)


def test_stock_arrival_resets_manual_preorder_xml_without_changing_price(db_session):
    from backend.app.preorder_modes import stock_arrived
    from xml.etree import ElementTree as ET
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        batch.quantity_remaining = 0
        product.kaspi_product_id = "123456789"
        product.merchant_sku = "actual-merchant-sku"
        feed = KaspiXmlFeed(workspace_id=1, active=True, generated_xml="", source_xml='<kaspi_catalog><offers><offer sku="actual-merchant-sku"><availabilities><availability storeId="PP1" available="yes" stockCount="5" preOrder="7"/></availabilities><cityprices><cityprice cityId="196220100">14000</cityprice></cityprices></offer></offers></kaspi_catalog>')
        db_session.add(feed)
        queue(db_session)
        assert not policy.enabled
        batch.quantity_remaining = 4
        assert stock_arrived(db_session, product.id)
        root = ET.fromstring(feed.generated_xml)
        availability = root.find(".//availability")
        assert availability.attrib["stockCount"] == "4"
        assert availability.attrib.get("preOrder", "0") == "0"
        assert root.find(".//cityprice").text == "14000"
        assert policy.enabled  # previous stock repricing settings resume


def test_completed_manual_preorder_cannot_be_enabled_from_other_pricing_tabs(db_session):
    from backend.app.preorder_api import assert_no_preorder_write
    with workspace_context(1):
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        done = _enroll_created_product(db_session, job=write, result=confirmed())
        product = db_session.get(Product, done["product_id"])
        with pytest.raises(HTTPException) as exc:
            assert_no_preorder_write(db_session, product=product)
        assert "Демпить этот товар" in exc.value.detail


@pytest.mark.parametrize("fresh", [False, True])
def test_preorder_fast_scan_waits_for_monitor_then_uses_requested_quantity(db_session, fresh):
    from backend.app.fast_dumping_service import complete_scan, claim_job, prepare_apply, utcnow
    from backend.app.suppliers import ProductBinding
    from backend.app.monitoring import SupplierOfferState
    from tests.test_fast_dumping import _market
    with workspace_context(1):
        queued = connect_preorder(PreorderRequest(reference=URL, price_kzt=14000,
            preorder_days=7, stock_count=11, dump_enabled=True,
            ozon_url="https://www.ozon.kz/product/example-123456789/"), db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        _enroll_created_product(db_session, job=write, result=confirmed(stock_count=11))
        if fresh:
            binding = db_session.scalar(select(ProductBinding))
            db_session.add(SupplierOfferState(workspace_id=1, supplier_product_id=binding.supplier_product_id,
                price=5000, currency="KZT", available=True, delivery_days=6,
                fingerprint="test", adapter_schema_version="test", observed_at=utcnow(), last_checked_at=utcnow()))
            db_session.flush()
        job = claim_job(db_session, workspace_id=1, agent_id="agent")
        assert job.status == "leased_scan"
        result = complete_scan(db_session, workspace_id=1, job_id=job.id, agent_id="agent",
            lease_token=job.lease_token, succeeded=True, market_payload=_market(own="14000", competitor="15000"))
        assert result["queued_apply"] is fresh
        if fresh:
            assert job.decision_json["stock_count"] == 11
            assert job.decision_json["preorder_days"] == 6
            apply_job = claim_job(db_session, workspace_id=1, agent_id="agent")
            assert apply_job.id == job.id
            ready = prepare_apply(db_session, workspace_id=1, job_id=job.id,
                agent_id="agent", lease_token=apply_job.lease_token)
            assert ready["ready"] and ready["stock_count"] == 11 and ready["preorder_days"] == 6
        else:
            assert result["status"] == "awaiting_supplier_refresh"
            assert not job.decision_json  # no zero/off or floor-only mutation


def test_existing_manual_preorder_without_toggle_is_reconciled_and_other_products_untouched(db_session):
    from backend.app.preorder_modes import reconcile_preorder_policies
    from backend.app.dumping_models import DumpingPolicy
    product, batch, fast, _ = _seed_fast_product(db_session)
    other, _, other_fast, _ = _seed_fast_product(db_session, workspace_id=3)
    with workspace_context(1):
        batch.quantity_remaining = 0
        classic = DumpingPolicy(workspace_id=1, product_id=product.id, enabled=True)
        item = ProductTestItem(workspace_id=1, input_reference="manual-preorder:123456789",
            kaspi_product_id="123456789", merchant_sku="test", name="Old manual preorder",
            product_id=product.id, kaspi_url=URL, offers_json={"manual_preorder": True}, active=False,
            status="preorder_connected")
        db_session.add_all([classic, item])
        db_session.commit()
        reconcile_preorder_policies(db_session, 1)
        assert not fast.enabled and not classic.enabled
        assert other_fast.enabled
        assert item.offers_json["resume_fast_on_stock"]


def test_preorder_agent_links_ozon_inside_workspace_even_with_unscoped_session(db_session):
    from backend.app.suppliers import Supplier, ProductBinding, SupplierProduct
    with workspace_context(1):
        db_session.add(Supplier(workspace_id=1, code="ozon", name="Ozon"))
        db_session.commit()
    db_session.info["include_all_workspaces"] = True
    with workspace_context(3):
        queued = connect_preorder(PreorderRequest(reference=URL, price_kzt=14000,
            preorder_days=7, dump_enabled=True, ozon_url="https://www.ozon.kz/product/example-123456789/"), db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        done = _enroll_created_product(db_session, job=write, result=confirmed())
        binding = db_session.scalar(select(ProductBinding).where(ProductBinding.product_id == done["product_id"]))
        supplier_product = db_session.get(SupplierProduct, binding.supplier_product_id)
        supplier = db_session.get(Supplier, supplier_product.supplier_id)
        assert supplier.workspace_id == supplier_product.workspace_id == binding.workspace_id == 3
    db_session.info.pop("include_all_workspaces")


def test_stock_transition_reprices_against_fifo_cost_instead_of_retrying_old_loss_price(db_session):
    from backend.app.preorder_modes import stock_arrived
    from backend.app.fast_dumping_service import queue_scan, claim_job, complete_scan, prepare_apply
    from tests.test_fast_dumping import _market
    product, batch, policy, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        product.kaspi_product_id = "123456789"
        product.merchant_sku = "actual-merchant-sku"
        batch.quantity_remaining = 0
        queued = queue(db_session)
        inspect(db_session, db_session.get(ProductTestJob, queued["job"]["id"]))
        write = db_session.scalar(select(ProductTestJob).where(ProductTestJob.job_type == "create_offer"))
        _enroll_created_product(db_session, job=write, result=confirmed())
        batch.quantity_remaining = 3
        batch.unit_cost = Decimal("20000")
        stock_arrived(db_session, product.id)
        queue_scan(db_session, policy=policy, workspace_id=1, reason="inventory_priority:received")
        job = claim_job(db_session, workspace_id=1, agent_id="agent")
        result = complete_scan(db_session, workspace_id=1, job_id=job.id, agent_id="agent",
            lease_token=job.lease_token, succeeded=True, market_payload=_market(own="14000", competitor="30000"))
        assert result["queued_apply"]
        assert job.decision_json["fulfillment_mode"] == "inventory"
        assert not job.decision_json.get("inventory_sync_only")
        apply = claim_job(db_session, workspace_id=1, agent_id="agent")
        ready = prepare_apply(db_session, workspace_id=1, job_id=apply.id,
            agent_id="agent", lease_token=apply.lease_token)
        assert ready["ready"] and ready["preorder_days"] == 0 and ready["stock_count"] == 3
        assert Decimal(ready["target_price_kzt"]) >= Decimal(job.decision_json["safe_floor_kzt"])
