from datetime import UTC, datetime, timedelta

from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.models import Product
from backend.app.monitoring import (
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from backend.app.product_supplier_binding_api import (
    ManualSupplierBindingCreate,
    ManualSupplierBindingUpdate,
    create_manual_supplier_binding,
    update_manual_supplier_binding,
)
from backend.app.supplier_identity import (
    canonical_supplier_product_identity,
    parse_supplier_url,
)
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_product_and_supplier(db_session):
    product = Product(
        kaspi_product_id="151877903",
        merchant_sku="151877903_110734483",
        name="GLS Pharmaceuticals Аргинин",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    return product, supplier


def test_supplier_url_identity_ignores_domain_slug_and_query_string() -> None:
    ru = parse_supplier_url(
        "https://www.ozon.ru/product/gls-arginin-51853964/?from=share"
    )
    kz = parse_supplier_url(
        "https://ozon.kz/product/drugoy-slug-51853964/?oos_search=false"
    )

    assert ru.external_id == "51853964"
    assert kz.external_id == ru.external_id
    assert canonical_supplier_product_identity(
        supplier_code="ozon",
        external_id="legacy-import-id",
        url="https://www.ozon.ru/product/gls-arginin-51853964/",
    ) == "51853964"


def test_manual_upsert_reuses_legacy_product_by_canonical_url(db_session) -> None:
    product, supplier = _seed_product_and_supplier(db_session)
    legacy = SupplierProduct(
        supplier_id=supplier.id,
        external_id="legacy-import-id",
        title="Аргинин Ozon",
        url="https://www.ozon.ru/product/old-slug-51853964/?from=import",
    )
    db_session.add(legacy)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=legacy.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    db_session.add(binding)
    db_session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(target)
    db_session.commit()

    result = create_manual_supplier_binding(
        product.id,
        ManualSupplierBindingCreate(
            url="https://www.ozon.kz/product/new-slug-51853964/",
            is_primary=True,
            run_initial_check=True,
        ),
        db_session,
    )

    assert result.supplier_product_id == legacy.id
    assert result.binding_id == binding.id
    assert result.monitor_target_id == target.id
    assert result.created_supplier_product is False
    assert db_session.query(SupplierProduct).count() == 1
    assert legacy.external_id == "51853964"
    assert db_session.query(BrowserAgentJob).count() == 1


def test_manual_upsert_disables_older_duplicate_binding(db_session) -> None:
    product, supplier = _seed_product_and_supplier(db_session)
    old_checked = datetime(2026, 7, 30, 2, 45, tzinfo=UTC)
    new_checked = datetime(2026, 7, 31, 13, 59, tzinfo=UTC)
    old_source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="legacy-arginin",
        title="Аргинин Ozon old",
        url="https://www.ozon.ru/product/arginin-51853964/",
        current_price=4998,
        in_stock=True,
        last_checked_at=old_checked,
    )
    canonical_source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title="Аргинин Ozon",
        url="https://www.ozon.kz/product/arginin-51853964/",
        current_price=None,
        in_stock=False,
        last_checked_at=new_checked,
    )
    db_session.add_all([old_source, canonical_source])
    db_session.flush()
    old_binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=old_source.id,
        status="active",
        is_primary=False,
        priority=0,
    )
    canonical_binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=canonical_source.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    db_session.add_all([old_binding, canonical_binding])
    db_session.flush()
    old_target = MonitorTarget(
        product_binding_id=old_binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=new_checked + timedelta(hours=2),
    )
    canonical_target = MonitorTarget(
        product_binding_id=canonical_binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=new_checked,
    )
    db_session.add_all([old_target, canonical_target])
    db_session.add_all(
        [
            SupplierOfferState(
                supplier_product_id=old_source.id,
                price=4998,
                currency="KZT",
                available=True,
                fingerprint="a" * 64,
                adapter_schema_version="ozon-browser-v12",
                observed_at=old_checked,
                last_checked_at=old_checked,
            ),
            SupplierOfferState(
                supplier_product_id=canonical_source.id,
                price=None,
                currency="KZT",
                available=False,
                stock=0,
                fingerprint="b" * 64,
                adapter_schema_version="ozon-browser-v13",
                observed_at=new_checked,
                last_checked_at=new_checked,
            ),
        ]
    )
    db_session.commit()

    result = create_manual_supplier_binding(
        product.id,
        ManualSupplierBindingCreate(
            url="https://www.ozon.ru/product/arginin-51853964/",
            is_primary=True,
            run_initial_check=False,
        ),
        db_session,
    )

    assert result.supplier_product_id == canonical_source.id
    assert old_binding.status == "disabled"
    assert old_binding.is_primary is False
    assert old_target.status == "disabled"
    assert canonical_binding.status == "active"
    assert canonical_target.status == "active"


def test_manual_source_replacement_keeps_one_binding_and_rejects_stale_jobs(
    db_session,
) -> None:
    product, supplier = _seed_product_and_supplier(db_session)
    sibling_product = Product(
        kaspi_product_id="151877904",
        merchant_sku="151877904_110734483",
        name="GLS Pharmaceuticals Аргинин, другая карточка",
        status="active",
    )
    db_session.add(sibling_product)
    db_session.flush()
    previous_source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title="Аргинин Ozon",
        url="https://www.ozon.ru/product/arginin-51853964/",
    )
    db_session.add(previous_source)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=previous_source.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    sibling_binding = ProductBinding(
        product_id=sibling_product.id,
        supplier_product_id=previous_source.id,
        status="active",
    )
    db_session.add_all((binding, sibling_binding))
    db_session.flush()
    checked_at = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=checked_at + timedelta(hours=1),
        last_checked_at=checked_at,
        consecutive_failures=3,
        lease_owner="scheduler",
        lease_token="scheduler-old-source",
        lease_until=checked_at + timedelta(minutes=5),
    )
    db_session.add(target)
    db_session.flush()
    observation = SupplierOfferObservation(
        supplier_product_id=previous_source.id,
        price=9282,
        old_price=None,
        currency="KZT",
        available=True,
        stock=1,
        delivery_days=4,
        seller="Ozon",
        fingerprint="c" * 64,
        adapter_schema_version="ozon-browser-v13",
        observed_at=checked_at,
    )
    queued_job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=previous_source.id,
        url=previous_source.url,
        status="queued",
    )
    leased_job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=previous_source.id,
        url=previous_source.url,
        status="leased",
        lease_owner="windows-agent",
        lease_token="windows-agent-old-source",
        lease_until=checked_at + timedelta(minutes=5),
    )
    db_session.add_all((observation, queued_job, leased_job))
    db_session.commit()

    result = update_manual_supplier_binding(
        product.id,
        binding.id,
        ManualSupplierBindingUpdate(
            url="https://www.wildberries.ru/catalog/245900111/detail.aspx",
            run_initial_check=True,
        ),
        db_session,
    )

    db_session.refresh(binding)
    db_session.refresh(sibling_binding)
    db_session.refresh(target)
    db_session.refresh(queued_job)
    db_session.refresh(leased_job)
    replacement = db_session.get(SupplierProduct, result.supplier_product_id)
    fresh_job = db_session.get(BrowserAgentJob, result.job_id)

    assert result.binding_id == binding.id
    assert result.monitor_target_id == target.id
    assert result.previous_supplier_product_id == previous_source.id
    assert result.supplier_product_id != previous_source.id
    assert result.supplier_code == "wb"
    assert result.cancelled_stale_jobs == 2
    assert result.queued_initial_check is True
    assert binding.supplier_product_id == replacement.id
    assert sibling_binding.supplier_product_id == previous_source.id
    assert db_session.query(ProductBinding).filter_by(product_id=product.id).count() == 1
    assert db_session.get(SupplierProduct, previous_source.id) is previous_source
    assert db_session.get(SupplierOfferObservation, observation.id) is observation
    assert queued_job.status == "failed"
    assert leased_job.status == "failed"
    assert leased_job.lease_owner is None
    assert leased_job.lease_token is None
    assert leased_job.lease_until is None
    assert fresh_job.status == "queued"
    assert fresh_job.monitor_target_id == target.id
    assert fresh_job.supplier_product_id == replacement.id
    assert fresh_job.url == str(result.url)
    assert target.status == "active"
    assert target.last_checked_at is None
    assert target.consecutive_failures == 0
    assert target.lease_owner is None
    assert target.lease_token is None
    assert target.lease_until is None


def test_manual_source_replacement_reuses_same_canonical_listing(db_session) -> None:
    product, supplier = _seed_product_and_supplier(db_session)
    source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title="Аргинин Ozon",
        url="https://www.ozon.ru/product/old-slug-51853964/?from=crm",
    )
    db_session.add(source)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=source.id,
        status="active",
    )
    db_session.add(binding)
    db_session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=datetime.now(UTC),
    )
    db_session.add(target)
    db_session.commit()

    result = update_manual_supplier_binding(
        product.id,
        binding.id,
        ManualSupplierBindingUpdate(
            url="https://ozon.kz/product/new-slug-51853964/",
            run_initial_check=False,
        ),
        db_session,
    )

    assert result.previous_supplier_product_id == source.id
    assert result.supplier_product_id == source.id
    assert result.created_supplier_product is False
    assert result.queued_initial_check is False
    assert source.url == "https://ozon.kz/product/new-slug-51853964/"
    assert db_session.query(SupplierProduct).count() == 1
