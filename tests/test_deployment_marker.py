from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_continuous_browser_monitoring_marker_is_exposed() -> None:
    assert APP_VERSION == "0.11.2"
    assert DEPLOYMENT_MARKER == "continuous-browser-monitoring-v1"
    assert app.version == APP_VERSION
