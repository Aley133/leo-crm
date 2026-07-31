from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from backend.app.browser_agent_api import (
    BrowserAgentResult,
    _normalize_known_business_outcome,
)
from backend.app.browser_agent_models import BrowserAgentJobStatus


def _supplier_job(url: str) -> SimpleNamespace:
    """Return the smallest object that satisfies the Browser Agent job contract."""

    return SimpleNamespace(
        supplier_product_id=51853964,
        monitor_target_id=None,
        url=url,
    )


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
        job=_supplier_job("https://www.wildberries.ru/catalog/51853964/detail.aspx"),
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
        job=_supplier_job("https://www.wildberries.ru/catalog/51853964/detail.aspx"),
        observed_at=datetime.now(UTC),
    )

    assert normalized is payload
    assert normalized.status == BrowserAgentJobStatus.FAILED.value


def test_legacy_ozon_out_of_stock_parse_error_is_a_successful_observation() -> None:
    observed_at = datetime(2026, 7, 31, 8, 30, tzinfo=UTC)
    payload = BrowserAgentResult(
        lease_token="c" * 24,
        status=BrowserAgentJobStatus.FAILED.value,
        error_code="AdapterParseError",
        error_message=(
            "Ozon browser page did not contain reliable structured offer data; "
            "body=Товар закончился"
        ),
    )

    normalized = _normalize_known_business_outcome(
        payload,
        job=_supplier_job("https://www.ozon.ru/product/example-51853964/"),
        observed_at=observed_at,
    )

    assert normalized.status == BrowserAgentJobStatus.SUCCEEDED.value
    assert normalized.payload is not None
    assert normalized.payload["available"] is False
    assert normalized.payload["stock"] == 0
    assert normalized.payload["adapter_schema_version"] == "ozon-browser-v13"
    assert normalized.payload["raw_metadata"]["source"] == (
        "ozon_browser_visible_out_of_stock"
    )


def test_ozon_recommendation_stock_text_does_not_close_a_purchasable_product() -> None:
    payload = BrowserAgentResult(
        lease_token="d" * 24,
        status=BrowserAgentJobStatus.FAILED.value,
        error_code="AdapterParseError",
        error_message=(
            "body=Добавить в корзину. В рекомендациях другой товар нет в наличии"
        ),
    )

    normalized = _normalize_known_business_outcome(
        payload,
        job=_supplier_job("https://www.ozon.ru/product/example-51853964/"),
        observed_at=datetime.now(UTC),
    )

    assert normalized is payload
    assert normalized.status == BrowserAgentJobStatus.FAILED.value
