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
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(encoding="utf-8")

    assert "MarketplaceOrder" not in script
    assert "PurchaseRequest" not in script
    assert "SqlAlchemyCommerceRepository" not in script
    assert "procurement_required_lines" in script
    assert "procurement_state" in script
