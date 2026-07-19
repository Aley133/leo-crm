from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_ozon_diagnostic_deployment_marker_is_exposed() -> None:
    assert APP_VERSION == "0.10.1"
    assert DEPLOYMENT_MARKER == "ozon-browser-v4-page-fingerprint"
    assert app.version == APP_VERSION
