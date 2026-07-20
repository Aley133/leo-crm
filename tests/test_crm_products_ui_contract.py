from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_products_route_is_exposed() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "backend" / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert '@router.get("/crm/products"' in ui
    assert 'FileResponse(STATIC_DIR / "products.html")' in ui
    assert 'href="/crm/products"' in dashboard


def test_products_page_uses_kaspi_catalog_contract() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert 'id="products-body"' in html
    assert 'id="filters"' in html
    assert 'id="only-unbound"' in html
    assert 'id="only-failures"' in html
    assert 'href="/crm/suppliers"' in html
    assert '"leo_crm_service_token"' in script
    assert '/api/catalog/products?' in script
    assert 'Authorization:`Bearer ${token}`' in script
    assert 'only_without_supplier' in script


def test_products_page_renders_kaspi_product_fields() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for field in (
        "product_id",
        "product_name",
        "kaspi_product_id",
        "merchant_sku",
        "brand",
        "product_status",
        "supplier_count",
        "best_supplier_name",
        "best_supplier_code",
        "best_supplier_price",
        "best_supplier_currency",
        "available_offer_count",
        "monitored_count",
        "failed_monitor_count",
        "last_checked_at",
    ):
        assert field in script

    assert 'href="/crm/products/${row.product_id}"' in script


def test_products_frontend_remains_read_only() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert 'method:"POST"' not in script
    assert 'method:"PUT"' not in script
    assert 'method:"PATCH"' not in script
    assert 'method:"DELETE"' not in script
