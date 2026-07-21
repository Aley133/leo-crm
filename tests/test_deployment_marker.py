from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_current_deployment_marker_is_exposed() -> None:
    assert APP_VERSION == "0.14.0"
    assert DEPLOYMENT_MARKER == "product-commerce-analytics-v1"
    assert app.version == APP_VERSION
