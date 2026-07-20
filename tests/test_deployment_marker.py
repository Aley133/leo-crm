from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_supplier_state_control_plane_marker_is_exposed() -> None:
    assert APP_VERSION == "0.13.0"
    assert DEPLOYMENT_MARKER == "supplier-state-control-plane-v1"
    assert app.version == APP_VERSION
