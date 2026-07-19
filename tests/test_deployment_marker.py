from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_auditable_pricing_marker_is_exposed() -> None:
    assert APP_VERSION == "0.12.0"
    assert DEPLOYMENT_MARKER == "auditable-pricing-recommendations-v1"
    assert app.version == APP_VERSION
