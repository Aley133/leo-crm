from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_orders_center_route_is_registered() -> None:
    source = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")

    assert '@router.get("/crm/orders"' in source
    assert 'STATIC_DIR / "orders.html"' in source


def test_orders_center_uses_commerce_and_purchase_apis() -> None:
    html = (ROOT / "backend" / "app" / "static" / "orders.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(encoding="utf-8")

    assert "Commerce Core" in html
    assert "Создать заявку на закупку" in script
    assert "/api/commerce/orders" in script
    assert "/api/purchases/from-marketplace-order" in script
    assert "idempotency_key" in script
    assert "marketplace_order_id" in script


def test_orders_center_does_not_duplicate_commerce_business_logic() -> None:
    html = (ROOT / "backend" / "app" / "static" / "orders.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(encoding="utf-8")

    assert "MarketplaceOrder" not in script
    assert "PurchaseRequest" not in script
    assert "SqlAlchemyCommerceRepository" not in script
    assert "procurement_required_lines" in script
    assert "procurement_state" in script
    assert "orders-coverage.js" not in html
    assert "incoming_reserved_units" in script


def test_orders_center_explains_preorder_coverage_per_product() -> None:
    html = (ROOT / "backend" / "app" / "static" / "orders.html").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "backend" / "app" / "static" / "orders.css").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(
        encoding="utf-8"
    )

    assert "procurementBreakdown" in script
    assert "incoming_reserved_quantity" in script
    assert "uncovered_quantity" in script
    assert "В пути ${incoming} шт. — они уже распределены по предзаказам." in script
    assert "Закажите ещё ${shortage} шт." in script
    assert '<details class="procurement-disclosure">' in script
    assert "Показать список товаров" in script
    assert "Скрыть список товаров" in script
    assert "merchant_sku" in script
    assert "external_product_id" in script
    assert 'class="procurement-advice hidden"' in html
    assert ".procurement-product" in styles
    assert ".procurement-disclosure[open]" in styles
