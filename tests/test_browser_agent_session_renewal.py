from __future__ import annotations

import asyncio

from backend.app.supplier_adapters.errors import (
    AdapterBlockedError,
    AdapterNetworkError,
)
from tools import browser_agent


def _job() -> dict:
    return {
        "id": 148251,
        "job_type": "supplier_product_observation",
        "supplier_product_id": 21,
        "url": "https://www.ozon.kz/product/test-123456789/",
        "lease_token": "lease-token-for-session-test",
    }


def test_blocked_result_requests_local_session_refresh(monkeypatch) -> None:
    async def blocked_run(job, adapters):
        raise AdapterBlockedError()

    completions: list[dict] = []

    def post_json(url, token, payload):
        completions.append(payload)
        return {"status": "failed"}

    monkeypatch.setattr(browser_agent, "_run_job", blocked_run)
    monkeypatch.setattr(browser_agent, "_post_json", post_json)

    result = asyncio.run(
        browser_agent._complete_job(
            api_url="https://crm.example",
            token="token",
            job=_job(),
            adapters={},
        )
    )

    assert result == browser_agent.SESSION_REFRESH_REQUIRED
    assert completions[0]["status"] == "failed"
    assert completions[0]["error_code"] == "AdapterBlockedError"


def test_network_failure_does_not_replace_local_session(monkeypatch) -> None:
    async def failed_run(job, adapters):
        raise AdapterNetworkError("temporary network failure")

    monkeypatch.setattr(browser_agent, "_run_job", failed_run)
    monkeypatch.setattr(
        browser_agent,
        "_post_json",
        lambda url, token, payload: {"status": "failed"},
    )

    result = asyncio.run(
        browser_agent._complete_job(
            api_url="https://crm.example",
            token="token",
            job=_job(),
            adapters={},
        )
    )

    assert result == "failed"


def test_worker_stops_claiming_after_session_refresh_signal(monkeypatch) -> None:
    claims = 0

    async def claim_one(**kwargs):
        nonlocal claims
        claims += 1
        return {"job": _job()}

    async def complete_job(**kwargs):
        return browser_agent.SESSION_REFRESH_REQUIRED

    monkeypatch.setattr(browser_agent, "_claim_one", claim_one)
    monkeypatch.setattr(browser_agent, "_complete_job", complete_job)
    refresh_required = asyncio.Event()

    asyncio.run(
        browser_agent._worker_loop(
            worker_number=1,
            api_url="https://crm.example",
            token="token",
            agent_id="agent",
            adapters={},
            poll_seconds=0.01,
            session_refresh_required=refresh_required,
        )
    )

    assert refresh_required.is_set()
    assert claims == 1
