from datetime import UTC, datetime
from pathlib import Path

from backend.app.main import app
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget
from backend.app.product_registry_api import list_products
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


ROOT = Path(__file__).resolve().parents[1]


def test_product_registry_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/product-registry/products" in paths
    assert "/api/product-registry/products/{product_id}" in paths


def test_product_registry_reuses_existing_product_aggregate() -> None:
    source = (ROOT / "backend" / "app" / "product_registry_api.py").read_text(encoding="utf-8")

    assert "from .models import MarketplaceOrderLine, Product, ProductStatus" in source
    assert "class ProductRegistryRow" in source
    assert "class ProductRegistryUpdate" in source
    for field in (
        "kaspi_product_id",
        "merchant_sku",
        "orders_count",
        "units_sold",
        "revenue_kzt",
        "supplier_count",
        "active_monitor_count",
        "available_offer_count",
        "failed_monitor_count",
        "best_supplier_name",
        "best_supplier_price",
        "last_checked_at",
    ):
        assert field in source
    assert "class ProductMaster" not in source
    assert '__tablename__ = "product_registry"' not in source


def test_product_registry_allows_only_core_product_updates() -> None:
    source = (ROOT / "backend" / "app" / "product_registry_api.py").read_text(encoding="utf-8")

    assert '@router.patch("/products/{product_id}"' in source
    assert "name: str | None" in source
    assert "brand: str | None" in source
    assert "merchant_sku: str | None" in source
    assert "status: ProductStatus | None" in source
    assert "db.commit()" in source


def test_product_filters_run_before_the_page_limit(db_session) -> None:
    first = Product(
        kaspi_product_id="FILTER-1",
        merchant_sku="FILTER-1",
        name="Bound without monitoring",
        status="active",
    )
    monitored = Product(
        kaspi_product_id="FILTER-2",
        merchant_sku="FILTER-2",
        name="Bound with failed monitoring",
        status="active",
    )
    unbound = Product(
        kaspi_product_id="FILTER-3",
        merchant_sku="FILTER-3",
        name="Unbound",
        status="active",
    )
    supplier = Supplier(code="filter-supplier", name="Filter supplier")
    db_session.add_all([first, monitored, unbound, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="filter-offer",
        title="Filter offer",
        url="https://example.test/filter-offer",
    )
    db_session.add(supplier_product)
    db_session.flush()
    first_binding = ProductBinding(
        product_id=first.id,
        supplier_product_id=supplier_product.id,
        status="active",
        priority=1,
    )
    monitored_binding = ProductBinding(
        product_id=monitored.id,
        supplier_product_id=supplier_product.id,
        status="active",
        priority=1,
    )
    db_session.add_all([first_binding, monitored_binding])
    db_session.flush()
    db_session.add(
        MonitorTarget(
            product_binding_id=monitored_binding.id,
            status="active",
            next_check_at=datetime.now(UTC),
            consecutive_failures=2,
        )
    )
    db_session.commit()

    common = {
        "q": None,
        "status": None,
        "limit": 1,
        "offset": 0,
        "db": db_session,
    }
    without_supplier = list_products(
        only_without_supplier=True,
        only_failures=False,
        only_monitored=False,
        **common,
    )
    failures = list_products(
        only_without_supplier=False,
        only_failures=True,
        only_monitored=False,
        **common,
    )
    monitored_rows = list_products(
        only_without_supplier=False,
        only_failures=False,
        only_monitored=True,
        **common,
    )

    assert [row.product_id for row in without_supplier] == [unbound.id]
    assert [row.product_id for row in failures] == [monitored.id]
    assert [row.product_id for row in monitored_rows] == [monitored.id]
