from pathlib import Path

from backend.app.main import app


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_products_route_and_assets_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/crm/workspace/products" in paths

    html = (ROOT / "backend" / "app" / "static" / "workspace-products.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "workspace-products.js").read_text(encoding="utf-8")

    assert 'href="/crm/workspace/products"' in html
    assert 'href="/crm/workspace/orders"' in html
    assert 'fetch(`/api/workspace/products?' in script
    assert 'localStorage.getItem(SESSION_KEY)' in script
    assert 'leo_workspace_session' in script


def test_workspace_orders_activates_products_navigation() -> None:
    html = (ROOT / "backend" / "app" / "static" / "workspace-orders.html").read_text(encoding="utf-8")
    assert '<a href="/crm/workspace/products">Товары</a>' in html
    assert 'class="disabled" href="#" aria-disabled="true" title="Переносится на workspace-изоляцию">Товары</a>' not in html
