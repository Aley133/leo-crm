from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


def test_event_driven_repricing_marker_is_exposed() -> None:
    assert APP_VERSION == "0.12.1"
    assert DEPLOYMENT_MARKER == "event-driven-repricing-v1"
    assert app.version == APP_VERSION
