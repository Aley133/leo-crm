from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.dumping_api import read_dumping_runtime
from backend.app.dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from backend.app.inventory_models import InventoryBatch
from backend.app.kaspi_competitor_agent_api import (
    AgentClaim,
    AgentComplete,
    claim_job,
    complete_job,
)
from backend.app.lease_engine import utc_now
from backend.app.models import Product


def _product(db_session, *, kaspi_id: str, name: str) -> Product:
    product = Product(
        kaspi_product_id=kaspi_id,
        merchant_sku=f"SKU-{kaspi_id}",
        name=name,
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def test_dumping_runtime_isolated_from_supplier_monitoring(db_session) -> None:
    now = utc_now()
    active_product = _product(
        db_session,
        kaspi_id="111222333",
        name="Товар в проверке Kaspi",
    )
    queued_product = _product(
        db_session,
        kaspi_id="444555666",
        name="Товар в очереди Kaspi",
    )
    active = DumpingRun(
        product_id=active_product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_type": "kaspi_competitor",
            "agent_id": "kaspi-notebook-w1",
            "stage": "local_scan",
            "leased_at": now.isoformat(),
            "lease_until": (now + timedelta(minutes=3)).isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    queued = DumpingRun(
        product_id=queued_product.id,
        status="queued_local",
        published=False,
        explanation_json={"agent_type": "kaspi_competitor"},
    )
    db_session.add_all([active, queued])
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert snapshot.queued_count == 1
    assert len(snapshot.active_runs) == 1
    assert snapshot.recent_results == []
    row = snapshot.active_runs[0]
    assert row.job_id == active.id
    assert row.status == "processing"
    assert row.agent_id == "kaspi-notebook-w1"
    assert row.product_name == "Товар в проверке Kaspi"
    assert "безопасную цену" in row.detail
    assert snapshot.latest_run is not None
    assert snapshot.latest_run.job_id == queued.id
    assert snapshot.latest_run.status == "queued"


def test_dumping_runtime_marks_expired_kaspi_lease(db_session) -> None:
    now = utc_now()
    product = _product(
        db_session,
        kaspi_id="777888999",
        name="Зависшая проверка Kaspi",
    )
    run = DumpingRun(
        product_id=product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_id": "kaspi-notebook-w2",
            "leased_at": (now - timedelta(minutes=4)).isoformat(),
            "lease_until": (now - timedelta(seconds=1)).isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    db_session.add(run)
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert snapshot.active_runs == []
    assert snapshot.recent_results[0].status == "lease_expired"
    assert "будет повторено автоматически" in snapshot.recent_results[0].detail


def test_dumping_runtime_shows_fresh_success_then_clears_it(db_session) -> None:
    now = utc_now()
    fresh_product = _product(
        db_session,
        kaspi_id="121212121",
        name="Свежий успешный результат",
    )
    old_product = _product(
        db_session,
        kaspi_id="343434343",
        name="Старый успешный результат",
    )
    db_session.add_all([
        DumpingRun(
            product_id=fresh_product.id,
            status="succeeded_local",
            published=True,
            explanation_json={
                "updated_at": (now - timedelta(minutes=1)).isoformat(),
            },
        ),
        DumpingRun(
            product_id=old_product.id,
            status="succeeded_local",
            published=True,
            explanation_json={
                "updated_at": (now - timedelta(minutes=4)).isoformat(),
            },
        ),
    ])
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert [row.product_name for row in snapshot.recent_results] == [
        "Свежий успешный результат"
    ]
    assert snapshot.recent_results[0].status == "succeeded"
    assert "XML обновлён" in snapshot.recent_results[0].detail


def test_dumping_runtime_drops_old_expired_leases_from_live_panel(db_session) -> None:
    now = utc_now()
    product = _product(
        db_session,
        kaspi_id="565656565",
        name="Давно прерванная проверка",
    )
    db_session.add(
        DumpingRun(
            product_id=product.id,
            status="leased_local",
            published=False,
            explanation_json={
                "leased_at": (now - timedelta(hours=2)).isoformat(),
                "lease_until": (now - timedelta(hours=1)).isoformat(),
                "updated_at": (now - timedelta(hours=1)).isoformat(),
            },
        )
    )
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert snapshot.active_runs == []
    assert snapshot.recent_results == []
    assert snapshot.latest_run is None


def test_kaspi_agent_reclaims_expired_job_and_can_finish_it(db_session) -> None:
    now = utc_now()
    product = _product(
        db_session,
        kaspi_id="787878787",
        name="Проверка после восстановления lease",
    )
    policy = DumpingPolicy(product_id=product.id, enabled=True)
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml="<kaspi_catalog/>",
        generated_xml="<kaspi_catalog/>",
        active=True,
    )
    run = DumpingRun(
        product_id=product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_id": "offline-agent-w1",
            "lease_token": "old-token-that-is-now-stale",
            "lease_attempt": 1,
            "leased_at": (now - timedelta(minutes=5)).isoformat(),
            "lease_until": (now - timedelta(minutes=2)).isoformat(),
            "updated_at": (now - timedelta(minutes=2)).isoformat(),
        },
    )
    db_session.add_all([policy, feed, run])
    db_session.commit()

    response = claim_job(
        AgentClaim(agent_id="online-agent-w1", hostname="BARWORK"),
        db_session,
    )
    db_session.refresh(run)

    assert response["job"]["id"] == run.id
    assert response["job"]["lease_token"] != "old-token"
    assert run.status == "leased_local"
    assert run.explanation_json["agent_id"] == "online-agent-w1"
    assert run.explanation_json["lease_attempt"] == 2
    assert run.explanation_json["previous_lease_until"]
    assert run.explanation_json["lease_recovered_at"]

    with pytest.raises(HTTPException) as stale_completion:
        complete_job(
            run.id,
            AgentComplete(
                lease_token="old-token-that-is-now-stale",
                status="failed",
                error_message="Запоздалый результат старого потока",
            ),
            db_session,
        )

    assert stale_completion.value.status_code == 409


def test_kaspi_agent_successfully_persists_decimal_result_in_one_commit(
    db_session,
    monkeypatch,
) -> None:
    product = _product(
        db_session,
        kaspi_id="919191919",
        name="Успешная проверка с точными ценами",
    )
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=True,
        minimum_profit_kzt=Decimal("1000"),
    )
    source_xml = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers>
      <offer sku='SKU-919191919'>
        <cityprices><cityprice cityId='750000000'>9999</cityprice></cityprices>
        <availability available='yes' preOrder='5'/>
      </offer>
    </offers></kaspi_catalog>"""
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml=source_xml,
        generated_xml=source_xml,
        active=True,
    )
    batch = InventoryBatch(
        product_id=product.id,
        received_at=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
        quantity_received=2,
        quantity_remaining=2,
        unit_cost=Decimal("2300"),
        source_name="Склад FIFO",
    )
    job = DumpingRun(
        product_id=product.id,
        dumping_policy_id=None,
        status="leased_local",
        published=False,
        explanation_json={
            "lease_token": "current-lease-token-123456",
            "stage": "local_scan",
        },
    )
    db_session.add_all([policy, feed, batch, job])
    db_session.commit()

    commit_count = 0
    original_commit = db_session.commit

    def counted_commit() -> None:
        nonlocal commit_count
        commit_count += 1
        original_commit()

    monkeypatch.setattr(db_session, "commit", counted_commit)

    response = complete_job(
        job.id,
        AgentComplete(
            lease_token="current-lease-token-123456",
            status="succeeded",
            own_price_kzt=Decimal("9999"),
            competitor_price_kzt=Decimal("8900"),
            competitor_name="Другой продавец",
            own_position=2,
            seller_count=4,
            product_url="https://kaspi.kz/shop/p/919191919/",
        ),
        db_session,
    )

    db_session.refresh(job)
    db_session.refresh(feed)
    decision_run = db_session.scalar(
        select(DumpingRun)
        .where(DumpingRun.id != job.id, DumpingRun.product_id == product.id)
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )

    assert response["status"] == "succeeded_local"
    assert commit_count == 1
    assert job.status == "succeeded_local"
    assert job.explanation_json["result"]["market"]["competitor_price_kzt"] == "8900"
    assert job.explanation_json["result"]["decision"]["target_price_kzt"] == "8899.00"
    assert decision_run is not None
    assert decision_run.target_price_kzt == Decimal("8899.00")
    assert ">8899<" in feed.generated_xml


def test_kaspi_agent_records_pricing_rejection_without_http_500(db_session) -> None:
    product = _product(
        db_session,
        kaspi_id="929292929",
        name="Товар без источника себестоимости",
    )
    policy = DumpingPolicy(product_id=product.id, enabled=True)
    feed = KaspiXmlFeed(
        merchant_id="merchant-1",
        source_filename="catalog.xml",
        source_xml="<kaspi_catalog/>",
        generated_xml="<kaspi_catalog/>",
        active=True,
    )
    job = DumpingRun(
        product_id=product.id,
        dumping_policy_id=None,
        status="leased_local",
        published=False,
        explanation_json={"lease_token": "pricing-error-token-123456"},
    )
    db_session.add_all([policy, feed, job])
    db_session.commit()

    response = complete_job(
        job.id,
        AgentComplete(
            lease_token="pricing-error-token-123456",
            status="succeeded",
            own_price_kzt=Decimal("9999"),
            competitor_price_kzt=Decimal("8900"),
            seller_count=2,
        ),
        db_session,
    )

    db_session.refresh(job)
    assert response == {"id": job.id, "status": "failed_local"}
    assert job.status == "failed_local"
    assert job.explanation_json["error_code"] == "dumping_decision_rejected"
    assert "Нет доступной партии" in job.explanation_json["error_message"]
