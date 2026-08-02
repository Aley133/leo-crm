from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


EXPECTED_APP_VERSION = "0.21.1"
EXPECTED_DEPLOYMENT_MARKER = "workspace-isolated-kaspi-xml-hotfix"


def test_application_metadata_contract() -> None:
    assert APP_VERSION == EXPECTED_APP_VERSION
    assert DEPLOYMENT_MARKER == EXPECTED_DEPLOYMENT_MARKER
    assert app.version == APP_VERSION
