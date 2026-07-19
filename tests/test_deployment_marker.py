from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_browser_agent_deployment_marker_is_exposed() -> None:
    assert APP_VERSION == "0.11.0"
    assert DEPLOYMENT_MARKER == "browser-agent-queue-v1"
    assert app.version == APP_VERSION
