from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.app.browser_agent_ingestion import (
    BrowserAgentResultError,
    normalized_offer_from_agent,
    persist_browser_agent_success,
)
from backend.app.browser_agent_models import BrowserAgentJob


def _job():
    return SimpleNamespace(id=41, supplier_product_id=17, monitor_target_id=3)


def test_browser_agent_job_model_links_monitor_target() -> None:
    columns = BrowserAgentJob.__table__.columns
    assert "monitor_target_id" in columns
    assert columns["monitor_target_id"].nullable is True
    foreign_keys = {fk.target_fullname for fk in columns["monitor_target_id"].foreign_keys}
    assert foreign_keys == {"monitor_targets.id"}


def test_agent_payload_becomes_normalized_offer_with_audit_metadata() -> None:
    offer = normalized_offer_from_agent(
        _job(),
        {
            "price": "3734.00",
            "old_price": "4100",
            "currency": "rub",
            "available": True,
            "stock": 7,
            "delivery_days": 2,
            "seller": "Ozon",
            "adapter_schema_version": "ozon-browser-structured-v4",
            "observed_at": datetime.now(UTC).isoformat(),
            "raw_metadata": {"source": "browser_json_ld"},
        },
    )

    assert str(offer.price) == "3734.00"
    assert str(offer.old_price) == "4100"
    assert offer.currency == "RUB"
    assert offer.available is True
    assert offer.stock == 7
    assert offer.delivery_days == 2
    assert offer.raw_metadata["execution_surface"] == "local_browser_agent"
    assert offer.raw_metadata["browser_agent_job_id"] == 41


def test_changed_agent_offer_triggers_recommendation_in_same_transaction() -> None:
    source = inspect.getsource(persist_browser_agent_success)
    observation_branch = source.index("if changed:")
    pricing_call = source.index("calculate_product_price(session, product_id=product_id)")
    assert pricing_call > observation_branch
    assert "session.commit()" not in source


def test_agent_payload_rejects_naive_observation_time() -> None:
    with pytest.raises(BrowserAgentResultError, match="timezone-aware"):
        normalized_offer_from_agent(
            _job(),
            {
                "price": "3734",
                "adapter_schema_version": "ozon-browser-structured-v4",
                "observed_at": "2026-07-19T12:00:00",
            },
        )


def test_agent_payload_rejects_negative_money() -> None:
    with pytest.raises(BrowserAgentResultError, match="must not be negative"):
        normalized_offer_from_agent(
            _job(),
            {
                "price": "-1",
                "adapter_schema_version": "ozon-browser-structured-v4",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
