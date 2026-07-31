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
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


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
    alert_call = source.index("enqueue_price_drop_alert(session, observation=observation)")
    pricing_call = source.index("calculate_product_price(session, product_id=product_id)")
    assert alert_call > observation_branch
    assert pricing_call > observation_branch
    assert alert_call < pricing_call
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


def test_out_of_stock_agent_result_updates_current_and_legacy_supplier_state(
    db_session,
) -> None:
    observed_at = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    supplier = Supplier(code="ozon", name="Ozon")
    product = Product(
        kaspi_product_id="123123123",
        merchant_sku="SKU-123123123",
        name="Проверка отсутствия",
        status="active",
    )
    db_session.add_all([supplier, product])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="ozon-123123123",
        title="Карточка Ozon",
        url="https://www.ozon.ru/product/ozon-123123123/",
        current_price="4998",
        delivery_days=8,
        in_stock=True,
    )
    db_session.add(supplier_product)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
    )
    db_session.add(binding)
    db_session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=observed_at,
    )
    db_session.add(target)
    db_session.flush()
    job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=supplier_product.id,
        url=supplier_product.url,
        status="leased",
        created_at=observed_at,
    )
    db_session.add(job)
    db_session.flush()

    _attempt_id, changed = persist_browser_agent_success(
        db_session,
        job=job,
        payload={
            "price": None,
            "old_price": None,
            "currency": None,
            "available": False,
            "stock": 0,
            "delivery_days": None,
            "seller": None,
            "adapter_schema_version": "ozon-browser-v13",
            "observed_at": observed_at.isoformat(),
            "raw_metadata": {"business_state": "out_of_stock"},
        },
        finished_at=observed_at,
    )

    state = db_session.query(SupplierOfferState).filter_by(
        supplier_product_id=supplier_product.id
    ).one()
    assert changed is True
    assert state.available is False
    assert state.price is None
    assert supplier_product.in_stock is False
    assert supplier_product.current_price is None
    assert supplier_product.delivery_days is None
