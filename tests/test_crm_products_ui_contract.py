from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_products_route_is_exposed() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "backend" / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert '@router.get("/crm/products"' in ui
    assert 'FileResponse(STATIC_DIR / "products.html")' in ui
    assert 'href="/crm/products"' in dashboard


def test_products_page_uses_product_registry_contract() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for element_id in (
        'id="products-body"',
        'id="filters"',
        'id="status"',
        'id="only-unbound"',
        'id="only-failures"',
        'id="only-monitored"',
        'id="summary-products"',
        'id="summary-units"',
        'id="summary-revenue"',
    ):
        assert element_id in html
    assert 'href="/crm/suppliers"' in html
    assert 'href="/crm/monitoring"' in html
    assert '"leo_crm_service_token"' in script
    assert '/api/product-registry/products?' in script
    assert 'Authorization:`Bearer ${token}`' in script
    assert 'only_without_supplier' in script
    assert 'only_failures' in script
    assert 'only_monitored' in script


def test_products_page_renders_product_registry_fields() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for field in (
        "product_id",
        "name",
        "kaspi_product_id",
        "merchant_sku",
        "brand",
        "status",
        "orders_count",
        "units_sold",
        "revenue_kzt",
        "supplier_count",
        "best_supplier_name",
        "best_supplier_price",
        "best_supplier_currency",
        "available_offer_count",
        "active_monitor_count",
        "failed_monitor_count",
        "last_checked_at",
    ):
        assert field in script

    assert 'href="/crm/products/${row.product_id}"' in script


def test_products_frontend_only_writes_through_explicit_xml_import() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert 'method:"POST"' in script
    assert "/api/product-registry/imports/xml/${action}" in script
    assert 'xmlRequest("preview", file)' in script
    assert 'xmlRequest("commit", selectedXmlFile)' in script
    assert 'method:"PUT"' not in script
    assert 'method:"PATCH"' not in script
    assert 'method:"DELETE"' not in script
