from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.app.browser_agent_models import BrowserAgent, BrowserAgentJobStatus
from backend.app.main import app
from backend.app.supplier_adapters.base import NormalizedOffer
from tools.browser_agent import _adapter_code_for_url, _run_job


ROOT = Path(__file__).resolve().parents[1]


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
            adapter_schema_version="ozon-browser-structured-v5",
            observed_at=datetime.now(UTC),
            raw_metadata={"source": "browser_json_ld"},
        )


def test_browser_agent_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/browser-agent/dispatch-due" in paths
    assert "/api/browser-agent/jobs" in paths
    assert "/api/browser-agent/claim" in paths
    assert "/api/browser-agent/heartbeat" in paths
    assert "/api/browser-agent/agents" in paths
    assert "/api/browser-agent/jobs/{job_id}/complete" in paths
    assert "/api/browser-agent/jobs/{job_id}" in paths
    assert "/api/browser-agent-registry/agents" in paths
    assert "/api/browser-agent-registry/agents/{agent_id}/events" in paths
    assert "/api/monitor-targets/{target_id}/queue-browser-agent" in paths


def test_local_agent_serializes_normalized_offer() -> None:
    result = asyncio.run(
        _run_job(
            {
                "supplier_product_id": 17,
                "url": "https://www.ozon.ru/product/example-17/",
            },
            {"ozon": FakeAdapter()},
        )
    )
    assert result["price"] == "3734"
    assert result["old_price"] == "4100"
    assert result["available"] is True
    assert result["delivery_days"] == 2
    assert result["raw_metadata"]["source"] == "browser_json_ld"


def test_local_agent_routes_marketplace_urls_to_distinct_adapters() -> None:
    assert _adapter_code_for_url("https://ozon.kz/product/example-17/") == "ozon"
    assert _adapter_code_for_url("https://www.ozon.ru/product/example-17/") == "ozon"
    assert _adapter_code_for_url("https://www.wildberries.ru/catalog/123/detail.aspx") == "wb"
    assert _adapter_code_for_url("https://wb.ru/catalog/123") == "wb"


def test_browser_agent_status_contract_is_explicit() -> None:
    assert {item.value for item in BrowserAgentJobStatus} == {
        "queued",
        "leased",
        "succeeded",
        "failed",
    }


def test_browser_agent_registry_model_is_persistent() -> None:
    assert BrowserAgent.__tablename__ == "browser_agents"
    columns = {column.name for column in BrowserAgent.__table__.columns}
    assert {
        "agent_id",
        "hostname",
        "platform",
        "version",
        "status",
        "current_job_id",
        "last_seen_at",
        "leases_taken",
        "leases_succeeded",
        "leases_failed",
    }.issubset(columns)


def test_deploy_schema_creates_browser_agent_registry() -> None:
    source = (ROOT / "tools" / "ensure_browser_agent_schema.py").read_text(encoding="utf-8")
    assert "BrowserAgent.__table__.create(bind=engine, checkfirst=True)" in source
    assert '"browser_agents"' in source


def test_agent_claim_sends_machine_identity() -> None:
    source = (ROOT / "tools" / "browser_agent.py").read_text(encoding="utf-8")
    assert '"hostname": socket.gethostname()' in source
    assert '"platform": platform.platform()' in source
    assert '"version": (os.getenv("BROWSER_AGENT_VERSION") or "dev").strip()' in source
    assert '"wb": WildberriesBrowserAccessAdapter(pool)' in source
    assert 'for supplier_code in ("ozon", "wb")' in source
