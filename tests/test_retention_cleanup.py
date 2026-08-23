from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from backend.app.browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from backend.app.models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceRawPayload,
)
from backend.app.monitoring import MonitorAttempt
from backend.app.retention_cleanup import (
    prune_browser_agent_history,
    prune_global_order_raw_payloads,
    prune_monitor_attempt_history,
)


def test_raw_payload_retention_keeps_order_and_latest_20(db_session):
    account = MarketplaceAccount(
        workspace_id=1,
        provider="kaspi",
        external_account_id="retention-test-account",
        display_name="Retention test",
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    order = MarketplaceOrder(
        workspace_id=1,
        marketplace_account_id=account.id,
        external_order_id="order-1",
        status="delivered",
        original_status="COMPLETED",
        currency="KZT",
        total_amount=Decimal("1000.00"),
    )
    db_session.add(order)
    db_session.flush()

    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(25):
        db_session.add(
            MarketplaceRawPayload(
                workspace_id=1,
                marketplace_account_id=account.id,
                payload_type="order",
                external_object_id="order-1",
                content_hash=f"hash-{index:02d}",
                payload_json={"version": index},
                received_at=base + timedelta(minutes=index),
            )
        )
    db_session.flush()

    removed = prune_global_order_raw_payloads(db_session, keep=20)
    db_session.flush()

    assert removed == 5
    assert db_session.get(MarketplaceOrder, order.id) is not None
    versions = list(
        db_session.scalars(
            select(MarketplaceRawPayload.payload_json)
            .where(MarketplaceRawPayload.external_object_id == "order-1")
            .order_by(MarketplaceRawPayload.received_at)
        ).all()
    )
    assert len(versions) == 20
    assert versions[0]["version"] == 5
    assert versions[-1]["version"] == 24


def test_browser_retention_never_deletes_active_and_keeps_recent_history(db_session):
    now = datetime(2026, 8, 23, tzinfo=UTC)
    for index in range(25):
        db_session.add(
            BrowserAgentJob(
                workspace_id=1,
                monitor_target_id=101,
                supplier_product_id=501,
                url="https://www.ozon.ru/product/test",
                status=BrowserAgentJobStatus.SUCCEEDED.value,
                result_payload=f'{{"index":{index}}}',
                created_at=now - timedelta(days=30, minutes=index),
                finished_at=now - timedelta(days=30, minutes=index),
            )
        )
    for index in range(2):
        db_session.add(
            BrowserAgentJob(
                workspace_id=1,
                monitor_target_id=101,
                supplier_product_id=501,
                url="https://www.ozon.ru/product/test",
                status=BrowserAgentJobStatus.FAILED.value,
                error_code="test",
                created_at=now - timedelta(days=1, minutes=index),
                finished_at=now - timedelta(days=1, minutes=index),
            )
        )
    queued = BrowserAgentJob(
        workspace_id=1,
        monitor_target_id=101,
        supplier_product_id=501,
        url="https://www.ozon.ru/product/test",
        status=BrowserAgentJobStatus.QUEUED.value,
    )
    leased = BrowserAgentJob(
        workspace_id=1,
        monitor_target_id=101,
        supplier_product_id=501,
        url="https://www.ozon.ru/product/test",
        status=BrowserAgentJobStatus.LEASED.value,
    )
    db_session.add_all((queued, leased))
    db_session.flush()
    active_ids = {queued.id, leased.id}

    removed = prune_browser_agent_history(
        db_session,
        now=now,
        retention_days=7,
        min_history_per_target=20,
        batch_size=3,
        max_rows=100,
    )
    db_session.flush()

    assert removed == 7
    assert all(db_session.get(BrowserAgentJob, job_id) is not None for job_id in active_ids)
    completed = int(
        db_session.scalar(
            select(func.count())
            .select_from(BrowserAgentJob)
            .where(BrowserAgentJob.status.in_(("succeeded", "failed")))
        )
        or 0
    )
    assert completed == 20


def test_monitor_attempt_retention_keeps_last_20_per_target(db_session):
    now = datetime(2026, 8, 23, tzinfo=UTC)
    for index in range(25):
        finished = now - timedelta(days=30, minutes=index)
        db_session.add(
            MonitorAttempt(
                workspace_id=1,
                monitor_target_id=77,
                lease_token=f"old-{index}",
                outcome="success",
                adapter_code="test",
                access_strategy="browser",
                started_at=finished - timedelta(seconds=1),
                finished_at=finished,
                duration_ms=1000,
                http_status=200,
            )
        )
    for index in range(2):
        finished = now - timedelta(days=1, minutes=index)
        db_session.add(
            MonitorAttempt(
                workspace_id=1,
                monitor_target_id=77,
                lease_token=f"recent-{index}",
                outcome="success",
                adapter_code="test",
                access_strategy="browser",
                started_at=finished - timedelta(seconds=1),
                finished_at=finished,
                duration_ms=1000,
                http_status=200,
            )
        )
    db_session.flush()

    removed = prune_monitor_attempt_history(
        db_session,
        now=now,
        retention_days=14,
        min_history_per_target=20,
        batch_size=4,
        max_rows=100,
    )
    db_session.flush()

    assert removed == 7
    remaining = int(
        db_session.scalar(
            select(func.count())
            .select_from(MonitorAttempt)
            .where(MonitorAttempt.monitor_target_id == 77)
        )
        or 0
    )
    assert remaining == 20
