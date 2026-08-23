from decimal import Decimal

from sqlalchemy import select

from backend.app.fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from backend.app.fast_dumping_supplier_events import _wake_fast_products_for_supplier
from backend.app.models import Product
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_fast_supplier(db_session):
    product = Product(
        kaspi_product_id="104398995",
        merchant_sku="104398995_246727281",
        name="Fast supplier lifecycle",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="ozon-fast-1",
        title="Ozon product",
        url="https://www.ozon.kz/product/fast-1/",
        current_price=Decimal("5000"),
        delivery_days=6,
        in_stock=True,
    )
    db_session.add(supplier_product)
    db_session.flush()
    db_session.add(
        ProductBinding(
            product_id=product.id,
            supplier_product_id=supplier_product.id,
            status="active",
            is_primary=True,
            priority=0,
        )
    )
    policy = FastDumpingPolicy(product_id=product.id, enabled=True)
    db_session.add(policy)
    db_session.flush()
    return product, supplier_product, policy


def test_changed_supplier_state_wakes_zero_fifo_fast_product(db_session) -> None:
    product, supplier_product, policy = _seed_fast_supplier(db_session)

    awakened = _wake_fast_products_for_supplier(
        db_session,
        supplier_product_id=supplier_product.id,
        changed=True,
    )
    db_session.flush()

    state = db_session.scalar(
        select(FastDumpingState).where(FastDumpingState.product_id == product.id)
    )
    assert awakened == 1
    assert state is not None
    assert state.active_job_id is not None
    job = db_session.get(FastDumpingJob, state.active_job_id)
    assert job is not None
    assert job.policy_id == policy.id
    assert job.status == "queued_scan"
    assert job.reason == "supplier_offer_changed"


def test_unchanged_supplier_state_does_not_create_fast_job(db_session) -> None:
    product, supplier_product, _policy = _seed_fast_supplier(db_session)

    awakened = _wake_fast_products_for_supplier(
        db_session,
        supplier_product_id=supplier_product.id,
        changed=False,
    )
    db_session.flush()

    assert awakened == 0
    assert db_session.scalar(
        select(FastDumpingState).where(FastDumpingState.product_id == product.id)
    ) is None
