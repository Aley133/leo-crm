import asyncio

from backend.app.main import APP_VERSION, DEPLOYMENT_MARKER, app, health


EXPECTED_APP_VERSION = "0.23.4"
EXPECTED_DEPLOYMENT_MARKER = "postgres-safe-dumping-recovery-lock"


def test_application_metadata_contract() -> None:
    assert APP_VERSION == EXPECTED_APP_VERSION
    assert DEPLOYMENT_MARKER == EXPECTED_DEPLOYMENT_MARKER
    assert app.version == APP_VERSION


def test_health_exposes_dumping_scheduler_state() -> None:
    payload = asyncio.run(health())

    assert "dumping_scheduler" in payload
    assert "recovered_count" in payload["dumping_scheduler"]
    assert "periodic_count" in payload["dumping_scheduler"]
