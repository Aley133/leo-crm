from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from backend.app.browser_agent_api import (
    BrowserAgentResult,
    _normalize_known_business_outcome,
)
from backend.app.browser_agent_models import BrowserAgentJobStatus


def test_verified_wildberries_out_of_stock_is_a_successful_observation() -> None:
    observed_at = datetime(2026, 7, 20, 22, 4, tzinfo=UTC)
    payload = BrowserAgentResult(
        lease_token="a" * 24,
        status=BrowserAgentJobStatus.FAILED.value,
        error_code="AdapterParseError",
        error_message="Wildberries product is out of stock",
    )

    normalized = _normalize_known_business_outcome(
        payload,
        job=SimpleNamespace(url="https://www.wildberries.ru/catalog/51853964/detail.aspx"),
        observed_at=observed_at,
    )

    assert normalized.status == BrowserAgentJobStatus.SUCCEEDED.value
    assert normalized.error_code is None
    assert normalized.error_message is None
    assert normalized.payload is not None
    assert normalized.payload["price"] is None
    assert normalized.payload["available"] is False
    assert normalized.payload["stock"] == 0
    assert normalized.payload["observed_at"] == observed_at.isoformat()
    assert normalized.payload["raw_metadata"]["business_state"] == "out_of_stock"


def test_unknown_parse_error_remains_failed() -> None:
    payload = BrowserAgentResult(
        lease_token="b" * 24,
        status=BrowserAgentJobStatus.FAILED.value,
        error_code="AdapterParseError",
        error_message="Wildberries visible purchase price was not found",
    )

    normalized = _normalize_known_business_outcome(
        payload,
        job=SimpleNamespace(url="https://www.wildberries.ru/catalog/51853964/detail.aspx"),
        observed_at=datetime.now(UTC),
    )

    assert normalized is payload
    assert normalized.status == BrowserAgentJobStatus.FAILED.value
