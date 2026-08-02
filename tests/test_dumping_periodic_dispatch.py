from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.app import dumping_competitor_worker
from backend.app.dumping_competitor_worker import (
    build_due_competitor_policies_statement,
    queue_due_competitor_jobs,
)
from backend.app.dumping_models import DumpingPolicy, DumpingRun
from backend.app.inventory_models import InventoryBatch
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
    assert "AWAITING_SUPPLIER_REFRESH" in sql
