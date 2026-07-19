from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_browser_agent_deployment_marker_is_exposed() -> None:
    assert APP_VERSION == "0.11.1"
    assert DEPLOYMENT_MARKER == "browser-agent-observation-ingestion-v1"
    assert app.version == APP_VERSION
