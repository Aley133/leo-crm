from pathlib import Path

from backend.app.main import app


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_inventory_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/inventory/{product_id}" in paths
    assert "/api/workspace/inventory/{product_id}/batches" in paths
    assert "/api/workspace/inventory/{product_id}/batches/{batch_id}" in paths
    assert "/crm/workspace/inventory" in paths


def test_workspace_inventory_is_fail_closed_by_product_ownership() -> None:
    source = (ROOT / "backend" / "app" / "workspace_inventory_api.py").read_text(encoding="utf-8")
    assert "Product.workspace_id == workspace_id" in source
    assert "principal.workspace_id" in source
    assert "require_workspace_principal" in source
    assert "create_inventory_batch" in source
    assert "rebuild_product_fifo" in source


def test_workspace_inventory_ui_uses_session_scoped_endpoints() -> None:
    html = (ROOT / "backend" / "app" / "static" / "workspace-inventory.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "workspace-inventory.js").read_text(encoding="utf-8")
    assert 'href="/crm/workspace/inventory"' in html
    assert 'href="/crm/workspace/products"' in html
    assert 'leo_workspace_session' in script
    assert '/api/workspace/products?limit=500' in script
    assert '/api/workspace/inventory/' in script
    assert 'leo_crm_service_token' not in script
