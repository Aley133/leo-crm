from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_detail_api_is_registered_and_protected() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    source = (ROOT / "backend" / "app" / "product_detail_api.py").read_text(encoding="utf-8")

    assert "from .product_detail_api import router as product_detail_router" in main
    assert "app.include_router(product_detail_router)" in main
    assert 'prefix="/api/products"' in source
    assert "dependencies=[Depends(require_service_token)]" in source
    assert '@router.get("/{product_id}/detail"' in source
    assert "ProductDetailResponse" in source


def test_product_detail_contract_contains_sales_bindings_and_history() -> None:
    source = (ROOT / "backend" / "app" / "product_detail_api.py").read_text(encoding="utf-8")

    for field in (
        "kaspi_product_id",
        "merchant_sku",
        "created_at",
        "updated_at",
        "sales",
        "orders_count",
        "units_sold",
        "revenue_kzt",
        "last_ordered_at",
        "bindings",
        "observations",
        "supplier_product_url",
        "monitor_status",
        "consecutive_failures",
        "price",
        "currency",
        "available",
        "delivery_days",
        "observed_at",
    ):
        assert field in source

    assert "MarketplaceOrderLine" in source
    assert "MarketplaceOrder" in source
    assert "observation_limit" in source
    assert "SupplierOfferObservation.observed_at.desc()" in source


def test_product_detail_ui_route_and_assets_are_exposed() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "product-detail.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")
    products_script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert '@router.get("/crm/products/{product_id}"' in ui
    assert "product-detail.html" in ui
    assert "/static/product-detail.css" in html
    assert "/static/product-detail.js" in html
    for element_id in (
        'id="kaspi-product-id"',
        'id="merchant-sku"',
        'id="product-brand"',
        'id="product-status"',
        'id="units-sold"',
        'id="orders-count"',
        'id="revenue-kzt"',
        'id="last-ordered-at"',
        'id="best-offer"',
        'id="bindings"',
        'id="observations-body"',
    ):
        assert element_id in html
    assert '/api/products/${productId}/detail?observation_limit=100' in script
    assert "renderBestOffer" in script
    assert "sales.units_sold" in script
    assert "sales.revenue_kzt" in script
    assert "leo_crm_service_token" in script
    assert 'href="/crm/products/${row.product_id}"' in products_script


def test_product_detail_distinguishes_render_outage_from_domain_errors() -> None:
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "const responseError" in script
    assert "[502, 503, 504]" in script
    assert "Сервис Render временно недоступен или перезапускается" in script
    assert 'response.status === 404' in script
    assert "Товар не найден." in script


def test_product_detail_frontend_remains_read_only() -> None:
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "method:" not in script
    assert 'fetch(`/api/products/${productId}/detail' in script
    assert "/run-now" not in script
    assert "method:\"PATCH\"" not in script
    assert "method:\"DELETE\"" not in script
