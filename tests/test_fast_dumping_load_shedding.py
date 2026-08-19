from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic

import backend.app.fast_dumping_agent_api as agent_api
import tools.kaspi_fast_dumping_agent as desktop_agent


def test_server_throttles_duplicate_claim_before_database_access() -> None:
    workspace_id = 9011

    class DatabaseMustNotBeUsed:
        def scalar(self, *_args, **_kwargs):
            raise AssertionError("throttled claim acquired a database connection")

    payload = agent_api.FastAgentIdentity(
        agent_id="old-agent-worker-2",
        workspace_id=workspace_id,
        merchant_uid="merchant",
        concurrency=2,
        version="1.0.0",
    )
    with agent_api._AGENT_GUARD_LOCK:
        agent_api._CLAIM_NOT_BEFORE[workspace_id] = monotonic() + 30
    try:
        result = agent_api.claim(payload, db=DatabaseMustNotBeUsed())
    finally:
        with agent_api._AGENT_GUARD_LOCK:
            agent_api._CLAIM_NOT_BEFORE.pop(workspace_id, None)

    assert result["job"] is None
    assert result["throttled"] is True
    assert result["retry_after_seconds"] >= 29


def test_server_claim_gate_bounds_busy_and_idle_polling() -> None:
    workspace_id = 9012
    with agent_api._AGENT_GUARD_LOCK:
        agent_api._CLAIM_NOT_BEFORE.pop(workspace_id, None)
    try:
        assert agent_api._reserve_claim_slot(workspace_id, now=100.0) == 0
        assert agent_api._reserve_claim_slot(workspace_id, now=100.1) == 2

        agent_api._defer_claims(workspace_id, seconds=60, now=102.0)
        assert agent_api._reserve_claim_slot(workspace_id, now=105.0) == 57
        assert agent_api._reserve_claim_slot(workspace_id, now=162.0) == 0
    finally:
        with agent_api._AGENT_GUARD_LOCK:
            agent_api._CLAIM_NOT_BEFORE.pop(workspace_id, None)


def test_agent_uses_one_worker_by_default_and_bounds_override(monkeypatch) -> None:
    monkeypatch.delenv("KASPI_FAST_DUMPING_CONCURRENCY", raising=False)
    assert desktop_agent._configured_concurrency() == 1

    monkeypatch.setenv("KASPI_FAST_DUMPING_CONCURRENCY", "8")
    assert desktop_agent._configured_concurrency() == 2

    monkeypatch.setenv("KASPI_FAST_DUMPING_CONCURRENCY", "invalid")
    assert desktop_agent._configured_concurrency() == 1


def test_agent_treats_cloudflare_520_as_transient_and_shares_backoff() -> None:
    assert {520, 521, 522, 523, 524}.issubset(
        desktop_agent.TRANSIENT_HTTP_STATUSES
    )
    desktop_agent._record_crm_success()
    try:
        first = desktop_agent._record_crm_failure(
            retry_after=None,
            now=100.0,
            jitter=0,
        )
        second = desktop_agent._record_crm_failure(
            retry_after=None,
            now=100.0,
            jitter=0,
        )

        assert first == 1.0
        assert second == 2.0
        assert desktop_agent._crm_gate_delay(now=100.0) == 2.0
    finally:
        desktop_agent._record_crm_success()

    assert desktop_agent._crm_gate_delay(now=100.0) == 0.0


def test_agent_serializes_crm_requests_behind_shared_circuit() -> None:
    source = Path(desktop_agent.__file__).read_text(encoding="utf-8")

    assert "_CRM_REQUEST_LOCK = asyncio.Lock()" in source
    assert "async with _CRM_REQUEST_LOCK" in source
    assert "await _wait_for_crm_gate()" in source
    assert "_acquire_single_instance(selected_workspace)" in source
    assert "ERROR_ALREADY_EXISTS" in source
    assert 'VERSION = "1.0.7"' in source
    assert "IDLE_POLL_MAX_SECONDS = 60" in source
    assert "VERIFY_POLL_SECONDS" not in source
    assert "_verify_price" not in source
    assert "separate verification after" in source


def test_agent_scans_by_kaspi_product_id_not_merchant_sku(monkeypatch) -> None:
    observed: list[dict] = []

    async def scan(**options):
        observed.append(options)
        return object()

    monkeypatch.setattr(desktop_agent, "scan_kaspi_competitors", scan)
    result = asyncio.run(
        desktop_agent._scan(
            {
                "kaspi_product_id": "105579941",
                "merchant_sku": "105579941_BARWORK-SKU",
                "city_id": "196220100",
                "zone_id": "Magnum_ZONE1",
                "name": "Test product",
                "brand": "LEO",
                "delivery_price_premium_kzt": 750,
                "delivery_advantage_days": 7,
            },
            "merchant-uid",
        )
    )

    assert result is not None
    assert observed[0]["kaspi_product_id"] == "105579941"
    assert observed[0]["own_merchant_sku"] == "105579941_BARWORK-SKU"
    assert observed[0]["delivery_price_premium_kzt"] == 750
    assert observed[0]["delivery_advantage_days"] == 7


def test_agent_reports_verification_scan_failure_without_hiding_it(monkeypatch) -> None:
    sent: list[dict] = []

    async def failed_scan(_job, _merchant_uid):
        raise RuntimeError("temporary Kaspi failure")

    async def post(_url, _token, payload, *, operation):
        sent.append({"payload": payload, "operation": operation})
        return {"status": "verification_retry"}

    monkeypatch.setattr(desktop_agent, "_scan", failed_scan)
    monkeypatch.setattr(desktop_agent, "_post_json_with_retry", post)

    asyncio.run(
        desktop_agent._process_verify(
            api_url="https://crm.example",
            token="token",
            job={"id": 77, "lease_token": "verification-lease-token"},
            agent_id="fast-agent",
            workspace_id=1,
            merchant_uid="merchant",
        )
    )

    assert sent[0]["payload"]["status"] == "failed"
    assert sent[0]["payload"]["observed_own_price_kzt"] is None
    assert sent[0]["payload"]["error_code"] == "RuntimeError"
    assert sent[0]["payload"]["error_message"] == "temporary Kaspi failure"
