import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from backend.app.dumping_models import DumpingPolicy, KaspiXmlFeed
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.product_detail_api import get_product_detail
from backend.app.product_registry_api import _product_rows
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "20260731_0026_reconcile_duplicate_supplier_bindings.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("duplicate_supplier_repair", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duplicate_supplier_migration_preserves_newest_binding_and_history(
    db_session,
) -> None:
    old_checked = datetime(2026, 7, 30, 2, 45, tzinfo=UTC)
    new_checked = datetime(2026, 7, 31, 13, 59, tzinfo=UTC)
    product = Product(
        kaspi_product_id="151877903",
        merchant_sku="151877903_110734483",
        name="GLS Pharmaceuticals Аргинин",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    old_source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="legacy-arginin",
        title=product.name,
        url="https://www.ozon.ru/product/arginin-51853964/",
        last_checked_at=old_checked,
    )
    new_source = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title=product.name,
        url="https://www.ozon.kz/product/arginin-51853964/",
        last_checked_at=new_checked,
    )
    db_session.add_all([old_source, new_source])
    db_session.flush()
    old_binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=old_source.id,
        status="active",
        is_primary=False,
        priority=0,
    )
    new_binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=new_source.id,
        status="active",
        is_primary=True,
        priority=0,
    )
    db_session.add_all([old_binding, new_binding])
    db_session.flush()
    old_target = MonitorTarget(
        product_binding_id=old_binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=new_checked,
    )
    new_target = MonitorTarget(
        product_binding_id=new_binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=new_checked,
    )
    db_session.add_all([old_target, new_target])
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
                supplier_product_id=new_source.id,
                price=None,
                currency="KZT",
                available=False,
                fingerprint="b" * 64,
                adapter_schema_version="ozon-browser-v13",
                observed_at=new_checked,
                last_checked_at=new_checked,
            ),
        ]
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        auto_publish_xml=True,
    )
    xml = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers>
      <offer sku='151877903_110734483'>
        <cityprices><cityprice cityId='750000000'>8148</cityprice></cityprices>
        <availability available='yes' preOrder='9'/>
      </offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        workspace_id=product.workspace_id,
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml=xml,
        generated_xml=xml,
        active=True,
    )
    db_session.add_all([policy, feed])
    db_session.commit()

    migration = _load_migration()
    migration._repair_duplicate_bindings(db_session.connection())
    db_session.expire_all()

    assert db_session.get(ProductBinding, new_binding.id).status == "active"
    assert db_session.get(ProductBinding, old_binding.id).status == "disabled"
    assert db_session.get(MonitorTarget, new_target.id).status == "active"
    assert db_session.get(MonitorTarget, old_target.id).status == "disabled"
    assert db_session.query(SupplierProduct).count() == 2
    assert db_session.query(SupplierOfferState).count() == 2
    db_session.refresh(feed)
    assert 'available="no"' in feed.generated_xml
    assert 'preOrder="0"' in feed.generated_xml

    registry_row = _product_rows(db_session, [db_session.get(Product, product.id)])[0]
    detail = get_product_detail(product.id, 100, db_session)
    assert registry_row.best_supplier_price is None
    assert registry_row.available_offer_count == 0
    assert detail.best_offer is None
    assert {binding.binding_status for binding in detail.bindings} == {
        "active",
        "disabled",
    }
