from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_api_separates_products_and_supplier_offers() -> None:
    source = (ROOT / "backend" / "app" / "catalog_api.py").read_text(encoding="utf-8")

    assert 'prefix="/api/catalog"' in source
    assert '@router.get("/products"' in source
    assert '@router.get("/supplier-offers"' in source
    assert "supplier_count" in source
    assert "best_supplier_price" in source
    assert "kaspi_product_name" in source
    assert "supplier_product_url" in source


def test_products_page_is_kaspi_centric() -> None:
    html = (ROOT / "backend" / "app" / "static" / "products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "products.js").read_text(encoding="utf-8")

    assert "Одна строка — одна карточка Kaspi" in html
    assert "/api/catalog/products" in script
    assert "supplier_count" in script
    assert "best_supplier_name" in script
    assert "/api/supplier-state/offers" not in script


def test_supplier_offers_page_links_back_to_kaspi_products() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "suppliers.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "suppliers.js").read_text(encoding="utf-8")

    assert '@router.get("/crm/suppliers"' in ui
    assert "Одна строка — одна карточка Ozon, WB" in html
    assert "/api/catalog/supplier-offers" in script
    assert "/crm/products/${row.product_id}" in script
    assert "only_unbound" in script
