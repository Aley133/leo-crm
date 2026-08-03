from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.app import dumping_competitor_worker
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.dumping_api import DumpingPolicyUpsert, upsert_dumping_policy
from backend.app.dumping_competitor_worker import (
    build_failed_recovery_candidates_statement,
    build_failed_recovery_lock_statement,
    build_legacy_recovery_candidates_statement,
    build_legacy_recovery_lock_statement,
    build_due_competitor_policies_statement,
    queue_failed_recovery_retries,
    queue_due_competitor_jobs,
    recover_legacy_auto_disabled_policies,
)
from backend.app.dumping_models import DumpingPolicy, DumpingRun
from backend.app.inventory_models import InventoryBatch
from backend.app.kaspi_competitor_agent_api import queue_competitor_job
from backend.app.models import Product


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def _policy(
    db_session,
    *,
    kaspi_product_id: str,
    enabled: bool = True,
    auto_publish_xml: bool = True,
    with_source: bool = True,
) -> DumpingPolicy:
    product = Product(
        kaspi_product_id=kaspi_product_id,
        merchant_sku=f"SKU-{kaspi_product_id}",
        name=f"Товар {kaspi_product_id}",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    policy = DumpingPolicy(
        product_id=product.id,
        enabled=enabled,
        auto_publish_xml=auto_publish_xml,
    )
    db_session.add(policy)
    db_session.flush()
    if with_source:
        db_session.add(
            InventoryBatch(
                product_id=product.id,
                received_at=NOW - timedelta(days=1),
                quantity_received=1,
                quantity_remaining=1,
                unit_cost=Decimal("1000"),
                source_name="Тестовый склад",
            )
        )
        db_session.flush()
    return policy


def _run(
    db_session,
    *,
    policy: DumpingPolicy,
    status: str,
    created_at: datetime,
) -> DumpingRun:
    run = DumpingRun(
        product_id=policy.product_id,
        dumping_policy_id=policy.id,
        status=status,
        published=False,
        explanation_json={},
        created_at=created_at,
    )
    db_session.add(run)
    db_session.flush()
    return run


def test_periodic_dispatch_queues_only_due_enabled_policies_once(db_session) -> None:
    never_checked = _policy(db_session, kaspi_product_id="100000001")
    old_check = _policy(db_session, kaspi_product_id="100000002")
    recent_check = _policy(db_session, kaspi_product_id="100000003")
    already_queued = _policy(db_session, kaspi_product_id="100000004")
    disabled = _policy(
        db_session,
        kaspi_product_id="100000005",
        enabled=False,
    )
    no_auto_publish = _policy(
        db_session,
        kaspi_product_id="100000006",
        auto_publish_xml=False,
    )
    no_procurement_source = _policy(
        db_session,
        kaspi_product_id="100000007",
        with_source=False,
    )
    awaiting_supplier_refresh = _policy(
        db_session,
        kaspi_product_id="100000008",
    )
    _run(
        db_session,
        policy=awaiting_supplier_refresh,
        status="awaiting_supplier_refresh",
        created_at=NOW - timedelta(minutes=30),
    )
    _run(
        db_session,
        policy=old_check,
        status="succeeded_local",
        created_at=NOW - timedelta(minutes=11),
    )
    _run(
        db_session,
        policy=recent_check,
        status="succeeded_local",
        created_at=NOW - timedelta(minutes=9),
    )
    existing = _run(
        db_session,
        policy=already_queued,
        status="queued_local",
        created_at=NOW - timedelta(hours=1),
    )

    job_ids = queue_due_competitor_jobs(db_session, now=NOW)
    duplicate_ids = queue_due_competitor_jobs(db_session, now=NOW)

    queued = db_session.scalars(
        select(DumpingRun)
        .where(DumpingRun.status == "queued_local")
        .order_by(DumpingRun.id)
    ).all()
    newly_queued = [run for run in queued if run.id != existing.id]

    assert len(job_ids) == 2
    assert duplicate_ids == ()
    assert {run.product_id for run in newly_queued} == {
        never_checked.product_id,
        old_check.product_id,
    }
    assert all(run.explanation_json["reason"] == "periodic_refresh" for run in newly_queued)
    assert all(
        run.explanation_json["agent_type"] == "kaspi_competitor"
        for run in newly_queued
    )
    assert recent_check.product_id not in {run.product_id for run in newly_queued}
    assert disabled.product_id not in {run.product_id for run in newly_queued}
    assert no_auto_publish.product_id not in {run.product_id for run in newly_queued}
    assert no_procurement_source.product_id not in {
        run.product_id for run in newly_queued
    }
    assert awaiting_supplier_refresh.product_id not in {
        run.product_id for run in newly_queued
    }


def test_queue_scheduler_dispatches_immediately_without_scanning_kaspi(
    monkeypatch,
) -> None:
    dispatches: list[bool] = []
    stop_event = asyncio.Event()

    def dispatch() -> tuple[int, ...]:
        dispatches.append(True)
        stop_event.set()
        return ()

    monkeypatch.setattr(
        dumping_competitor_worker,
        "dispatch_due_competitor_jobs",
        dispatch,
    )

    asyncio.run(dumping_competitor_worker._scheduler_loop(stop_event))

    assert dispatches == [True]
    assert dumping_competitor_worker.SCHEDULER_LAST_RUN["status"] == "completed"


def test_periodic_dispatch_uses_postgresql_skip_locked_and_active_job_guard() -> None:
    statement = build_due_competitor_policies_statement(now=NOW)
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "DUMPING_POLICIES.ENABLED IS TRUE" in sql
    assert "DUMPING_POLICIES.AUTO_PUBLISH_XML IS TRUE" in sql
    assert "NOT (EXISTS" in sql
    assert "QUEUED_LOCAL" in sql
    assert "LEASED_LOCAL" in sql


def test_legacy_recovery_uses_postgres_safe_two_phase_locking() -> None:
    candidate_sql = str(
        build_legacy_recovery_candidates_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    lock_sql = str(
        build_legacy_recovery_lock_statement(policy_ids=(1, 2)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "GROUP BY" in candidate_sql
    assert "FOR UPDATE" not in candidate_sql
    assert "GROUP BY" not in lock_sql
    assert "FOR UPDATE SKIP LOCKED" in lock_sql


def test_failed_recovery_retry_uses_postgres_safe_two_phase_locking() -> None:
    candidate_sql = str(
        build_failed_recovery_candidates_statement(now=NOW).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    lock_sql = str(
        build_failed_recovery_lock_statement(product_ids=(1, 2)).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "GROUP BY" in candidate_sql
    assert "FOR UPDATE" not in candidate_sql
    assert "GROUP BY" not in lock_sql
    assert "FOR UPDATE SKIP LOCKED" in lock_sql


def test_completed_supplier_job_self_heals_waiting_dumping_gate(db_session) -> None:
    policy = _policy(db_session, kaspi_product_id="100000009")
    supplier_job = BrowserAgentJob(
        supplier_product_id=999,
        url="https://supplier.test/product/999",
        status="succeeded",
    )
    db_session.add(supplier_job)
    db_session.flush()
    waiting = DumpingRun(
        product_id=policy.product_id,
        dumping_policy_id=policy.id,
        status="awaiting_supplier_refresh",
        published=True,
        explanation_json={"supplier_refresh_job_id": supplier_job.id},
        created_at=NOW - timedelta(minutes=30),
    )
    db_session.add(waiting)
    db_session.flush()

    job_ids = queue_due_competitor_jobs(db_session, now=NOW)

    assert len(job_ids) == 1
    assert waiting.status == "supplier_refresh_ready"
    queued = db_session.get(DumpingRun, job_ids[0])
    assert queued is not None
    assert queued.product_id == policy.product_id


def test_manual_request_promotes_existing_periodic_job(db_session) -> None:
    policy = _policy(db_session, kaspi_product_id="100000010")
    existing = DumpingRun(
        product_id=policy.product_id,
        dumping_policy_id=policy.id,
        status="queued_local",
        published=False,
        explanation_json={"reason": "periodic_refresh"},
        created_at=NOW,
    )
    db_session.add(existing)
    db_session.flush()

    returned = queue_competitor_job(
        db_session,
        product_id=policy.product_id,
        reason="manual",
    )

    assert returned.id == existing.id
    assert existing.explanation_json["reason"] == "manual"
    assert existing.explanation_json["priority"] == "interactive"


def test_legacy_automatic_suspension_recovers_when_cost_source_returns(
    db_session,
) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="120199530_817407461",
        enabled=False,
    )
    suspended = _run(
        db_session,
        policy=policy,
        status="suspended_seller_removed",
        created_at=NOW - timedelta(hours=4),
    )
    _run(
        db_session,
        policy=policy,
        status="failed_local",
        created_at=NOW - timedelta(hours=1),
    )

    job_ids = recover_legacy_auto_disabled_policies(db_session, now=NOW)
    repeated = recover_legacy_auto_disabled_policies(db_session, now=NOW)

    assert len(job_ids) == 1
    assert repeated == ()
    assert policy.enabled is True
    queued = db_session.get(DumpingRun, job_ids[0])
    assert queued is not None
    assert queued.product_id == policy.product_id
    assert queued.explanation_json == {
        "reason": "automatic_policy_recovery",
        "agent_type": "kaspi_competitor",
        "scheduled_at": NOW.isoformat(),
        "recovered_from": suspended.status,
    }


def test_unclassified_legacy_disabled_policy_recovers_when_source_exists(
    db_session,
) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="120199530_817407462",
        enabled=False,
    )
    _run(
        db_session,
        policy=policy,
        status="failed_local",
        created_at=NOW - timedelta(hours=1),
    )

    job_ids = recover_legacy_auto_disabled_policies(db_session, now=NOW)

    assert len(job_ids) == 1
    assert policy.enabled is True
    queued = db_session.get(DumpingRun, job_ids[0])
    assert queued is not None
    assert queued.explanation_json["reason"] == "automatic_policy_recovery"
    assert queued.explanation_json["recovered_from"] == "legacy_unclassified"


def test_explicitly_disabled_policy_is_not_auto_recovered(db_session) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="121221211_440769902",
        enabled=False,
    )
    _run(
        db_session,
        policy=policy,
        status="suspended_seller_removed",
        created_at=NOW - timedelta(hours=4),
    )
    _run(
        db_session,
        policy=policy,
        status="policy_disabled_manual",
        created_at=NOW - timedelta(hours=1),
    )

    assert recover_legacy_auto_disabled_policies(db_session, now=NOW) == ()
    assert policy.enabled is False


def test_policy_api_audits_manual_disable_for_future_recovery(db_session) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="121221211_440769903",
        enabled=True,
    )

    upsert_dumping_policy(
        policy.product_id,
        DumpingPolicyUpsert(
            enabled=False,
            minimum_profit_kzt=Decimal("1000"),
        ),
        db_session,
    )

    db_session.refresh(policy)
    latest_state = db_session.scalar(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == policy.product_id,
            DumpingRun.status.in_(
                ("suspended_seller_removed", "policy_disabled_manual")
            ),
        )
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    assert policy.enabled is False
    assert latest_state is not None
    assert latest_state.status == "policy_disabled_manual"
    assert latest_state.explanation_json == {
        "reason": "manual_policy_disable",
        "automatic_recovery": False,
    }


def test_failed_automatic_recovery_retries_after_one_minute(db_session) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="121221211_440769904",
        enabled=True,
    )
    failed = DumpingRun(
        product_id=policy.product_id,
        dumping_policy_id=policy.id,
        status="failed_local",
        published=False,
        explanation_json={
            "reason": "automatic_policy_recovery",
            "error_code": "temporary_agent_error",
        },
        created_at=NOW - timedelta(seconds=61),
    )
    db_session.add(failed)
    db_session.flush()

    job_ids = queue_failed_recovery_retries(db_session, now=NOW)
    repeated = queue_failed_recovery_retries(db_session, now=NOW)

    assert len(job_ids) == 1
    assert repeated == ()
    retry = db_session.get(DumpingRun, job_ids[0])
    assert retry is not None
    assert retry.status == "queued_local"
    assert retry.explanation_json["reason"] == "automatic_policy_recovery_retry"
    assert retry.explanation_json["recovery_retry_attempt"] == 1
    assert retry.explanation_json["previous_job_id"] == failed.id


def test_failed_automatic_recovery_stops_after_three_retries(db_session) -> None:
    policy = _policy(
        db_session,
        kaspi_product_id="121221211_440769905",
        enabled=True,
    )
    db_session.add(
        DumpingRun(
            product_id=policy.product_id,
            dumping_policy_id=policy.id,
            status="failed_local",
            published=False,
            explanation_json={
                "reason": "automatic_policy_recovery_retry",
                "recovery_retry_attempt": 3,
                "error_code": "persistent_agent_error",
            },
            created_at=NOW - timedelta(seconds=61),
        )
    )
    db_session.flush()

    assert queue_failed_recovery_retries(db_session, now=NOW) == ()
