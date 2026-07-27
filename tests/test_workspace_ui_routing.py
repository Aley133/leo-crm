from backend.app.main import app


def test_workspace_and_legacy_crm_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/crm" in paths
    assert "/crm/legacy" in paths
    assert "/crm/workspace/orders" in paths
    assert "/crm/account" in paths


def test_workspace_static_assets_exist() -> None:
    from backend.app.ui import STATIC_DIR

    for name in (
        "crm-gateway.html",
        "crm-gateway.js",
        "workspace-orders.html",
        "workspace-orders.js",
    ):
        assert (STATIC_DIR / name).is_file(), name
