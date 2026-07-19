from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from backend.app.browser_agent_models import BrowserAgentJobStatus
from backend.app.main import app
from backend.app.supplier_adapters.base import NormalizedOffer
from tools.browser_agent import _run_job


class FakeAdapter:
    async def fetch(self, request):
        assert request.supplier_product_id == 17
        assert request.url == "https://www.ozon.ru/product/example-17/"
        assert request.external_id == "browser-agent-17"
        return NormalizedOffer(
            supplier_product_id=17,
            price=Decimal("3734"),
            old_price=Decimal("4100"),
            available=True,
            stock=None,
            delivery_days=2,
            seller="Ozon",
            adapter_schema_version="ozon-browser-structured-v4",
            observed_at=datetime.now(UTC),
            raw_metadata={"source": "browser_json_ld"},
        )


def test_browser_agent_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/browser-agent/dispatch-due" in paths
    assert "/api/browser-agent/jobs" in paths
    assert "/api/browser-agent/claim" in paths
    assert "/api/browser-agent/jobs/{job_id}/complete" in paths
    assert "/api/browser-agent/jobs/{job_id}" in paths
    assert "/api/monitor-targets/{target_id}/queue-browser-agent" in paths


def test_local_agent_serializes_normalized_offer() -> None:
    result = asyncio.run(
        _run_job(
            {
                "supplier_product_id": 17,
                "url": "https://www.ozon.ru/product/example-17/",
            },
            FakeAdapter(),
        )
    )
    assert result["price"] == "3734"
    assert result["old_price"] == "4100"
    assert result["available"] is True
    assert result["delivery_days"] == 2
    assert result["raw_metadata"]["source"] == "browser_json_ld"


def test_browser_agent_status_contract_is_explicit() -> None:
    assert {item.value for item in BrowserAgentJobStatus} == {
        "queued",
        "leased",
        "succeeded",
        "failed",
    }
