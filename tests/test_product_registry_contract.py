from pathlib import Path

from backend.app.main import app


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
