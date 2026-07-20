from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_products_route_is_exposed() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "backend" / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert '@router.get("/crm/products"' in ui
    assert 'FileResponse(STATIC_DIR / "products.html")' in ui
    assert 'href="/crm/products"' in dashboard


def test_products_page_uses_supplier_state_control_plane() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert 'id="products-body"' in html
    assert 'id="filters"' in html
    assert 'id="only-stale"' in html
    assert 'id="only-failures"' in html
    assert 'id="availability"' in html
    assert 'id="supplier-code"' in html
    assert '"leo_crm_service_token"' in script
    assert '/api/supplier-state/offers?' in script
    assert 'Authorization:`Bearer ${token}`' in script


def test_products_page_renders_operational_fields() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    for field in (
        "product_name",
        "kaspi_product_id",
        "supplier_name",
        "supplier_product_url",
        "price",
        "currency",
        "delivery_days",
        "available",
        "stock",
        "monitor_status",
        "consecutive_failures",
        "last_checked_at",
        "is_stale",
    ):
        assert field in script


def test_products_frontend_remains_read_only() -> None:
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert 'method:"POST"' not in script
    assert 'method:"PUT"' not in script
    assert 'method:"PATCH"' not in script
    assert 'method:"DELETE"' not in script
