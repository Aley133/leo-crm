from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_product_card_preserves_full_inventory_surface() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "workspace-product-detail.html").read_text(encoding="utf-8")
    inventory = (ROOT / "backend" / "app" / "static" / "workspace-product-inventory.js").read_text(encoding="utf-8")
    products = (ROOT / "backend" / "app" / "static" / "workspace-products.js").read_text(encoding="utf-8")

    assert '@router.get("/crm/workspace/products/{product_id}"' in ui
    assert "Партии склада" in html
    assert "Экономика одной продажи" in html
    assert "Источники закупки" in html
    assert "История наблюдений" in html
    assert "/api/workspace/inventory/" in inventory
    assert "leo_workspace_session" in inventory
    assert 'href="/crm/workspace/products/${row.product_id}"' in products
