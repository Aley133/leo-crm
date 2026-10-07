import pytest
from backend.app.fast_dumping_models import FastDumpingJob
from backend.app.fast_dumping_supply_guard import disable_empty_products
from backend.app.suppliers import Supplier, SupplierProduct, ProductBinding
from backend.app.models import Product
from backend.app.product_test_models import ProductTestItem  # noqa: F401
from backend.app.workspace_context import workspace_context
from backend.app import fast_dumping_service as svc
from tests.test_fast_dumping import _seed_fast_product


def empty_product(db_session, workspace_id=1):
    product, batch, policy, state = _seed_fast_product(db_session, quantity=1, workspace_id=workspace_id)
    with workspace_context(workspace_id):
        batch.quantity_remaining = 0
        db_session.flush()
    return product, batch, policy, state


@pytest.mark.parametrize("status", ["queued_scan", "leased_scan", "queued_apply"])
def test_empty_unbound_product_leaves_queue(db_session, status):
    product, batch, policy, state = empty_product(db_session)
    with workspace_context(1):
        job, _ = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason="scheduled")
        job.status = status
        db_session.flush()
        assert disable_empty_products(db_session, 1) == 1
        assert not policy.enabled
        assert state.status == "paused" and state.next_scan_at is None
        assert state.active_job_id is None and job.status == "cancelled"
        assert svc.schedule_due_scans(db_session, workspace_id=1) == 0


@pytest.mark.parametrize("status", ["leased_apply", "queued_verify", "leased_verify"])
def test_empty_product_keeps_already_sent_operation_single_flight(db_session, status):
    product, batch, policy, state = empty_product(db_session)
    with workspace_context(1):
        job, _ = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason="scheduled")
        job.status = status
        job.lease_token = "existing-token"
        db_session.flush()
        disable_empty_products(db_session, 1)
        assert not policy.enabled
        assert state.active_job_id == job.id
        assert job.status == status and job.lease_token == "existing-token"


def test_warehouse_stock_retains_policy(db_session):
    product, batch, policy, state = _seed_fast_product(db_session, quantity=1)
    with workspace_context(1):
        assert disable_empty_products(db_session, 1) == 0
        assert policy.enabled


@pytest.mark.parametrize("alias", [False, True])
def test_bound_supplier_without_available_price_retains_policy(db_session, alias):
    product, batch, policy, state = empty_product(db_session)
    with workspace_context(1):
        bound_product = product
        if alias:
            bound_product = Product(kaspi_product_id="supplier-alias", merchant_sku="supplier-alias",
                                    name="Alias", inventory_owner_product_id=product.id)
            db_session.add(bound_product)
            db_session.flush()
        supplier = Supplier(code="test-supply", name="Supplier")
        db_session.add(supplier)
        db_session.flush()
        offer = SupplierProduct(supplier_id=supplier.id, external_id="empty-price",
                                title="No price", url="https://example.com/item", in_stock=False)
        db_session.add(offer)
        db_session.flush()
        db_session.add(ProductBinding(product_id=bound_product.id,
                                     supplier_product_id=offer.id, status="confirmed"))
        db_session.flush()
        assert disable_empty_products(db_session, 1) == 0
        assert policy.enabled


def test_inventory_exhaustion_disables_immediately_without_scanning(db_session):
    from backend.app.fast_dumping_xml_guard import _sync_product_inventory_to_feed
    product, batch, policy, state = _seed_fast_product(db_session, quantity=1)
    with workspace_context(1):
        batch.quantity_remaining = 0
        db_session.flush()
        _sync_product_inventory_to_feed(db_session, product_id=product.id, reason="sold_last_unit")
        assert not policy.enabled and state.active_job_id is None
        assert state.status == "paused"


def test_other_workspace_is_untouched(db_session):
    product, batch, policy, state = empty_product(db_session, workspace_id=3)
    with workspace_context(1):
        assert disable_empty_products(db_session, 1) == 0
    assert policy.enabled
