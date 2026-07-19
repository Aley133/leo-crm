from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_local_browser_verification_marker_is_exposed() -> None:
    assert APP_VERSION == "0.12.2"
    assert DEPLOYMENT_MARKER == "local-browser-verification-v1"
    assert app.version == APP_VERSION
