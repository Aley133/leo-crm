from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import marketplace_api
from backend.app.main import app
from backend.app.marketplace_sync import MarketplaceSyncResult


SERVICE_TOKEN = "test-service-token"
AUTH_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}


@dataclass
class StubTransport:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


def test_kaspi_status_requires_service_token(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", SERVICE_TOKEN)
    client = TestClient(app)

    response = client.get("/api/marketplaces/kaspi/status")

    assert response.status_code == 401


def test_kaspi_status_reports_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("KASPI_API_TOKEN", raising=False)
    client = TestClient(app)

    response = client.get("/api/marketplaces/kaspi/status", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "state": "not_configured",
        "detail": "KASPI_API_TOKEN is not configured",
    }


def test_sync_returns_controlled_503_when_kaspi_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", SERVICE_TOKEN)
    monkeypatch.delenv("KASPI_API_TOKEN", raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/marketplaces/kaspi/orders/sync-page",
        params={"marketplace_account_id": 1},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "KASPI_API_TOKEN is not configured"


def test_sync_uses_bounded_application_entrypoint_and_closes_transport(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", SERVICE_TOKEN)
    transport = StubTransport()
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        marketplace_api,
        "build_kaspi_order_transport",
        lambda: transport,
    )

    def fake_sync(session_factory, supplied_transport, *, marketplace_account_id: int, limit: int):
        assert session_factory is marketplace_api.SessionLocal
        assert supplied_transport is transport
        calls.append((marketplace_account_id, limit))
        return MarketplaceSyncResult(
            execution_id=uuid4(),
            fetched_count=2,
            imported_count=1,
            updated_count=1,
            next_cursor="2",
        )

    monkeypatch.setattr(marketplace_api, "sync_kaspi_order_page", fake_sync)
    client = TestClient(app)

    response = client.post(
        "/api/marketplaces/kaspi/orders/sync-page",
        params={"marketplace_account_id": 7, "limit": 25},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["fetched_count"] == 2
    assert response.json()["imported_count"] == 1
    assert response.json()["updated_count"] == 1
    assert response.json()["next_cursor"] == "2"
    assert calls == [(7, 25)]
    assert transport.closed is True
