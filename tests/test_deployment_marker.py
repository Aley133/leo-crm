from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app


EXPECTED_APP_VERSION = "0.22.2"
EXPECTED_DEPLOYMENT_MARKER = "supplier-preorder-xml-stock-capacity"


def test_application_metadata_contract() -> None:
    assert APP_VERSION == EXPECTED_APP_VERSION
    assert DEPLOYMENT_MARKER == EXPECTED_DEPLOYMENT_MARKER
    assert app.version == APP_VERSION
