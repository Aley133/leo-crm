from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend.app.lease_engine import (
    claim_due_targets,
    due_target_statement,
    leased_target_statement,
    release_target,
    reschedule_failure,
    reschedule_success,
)
from backend.app.models import Product
from backend.app.monitoring import MonitorStatus, MonitorTarget
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_target(session: Session, *, next_check_at: datetime, interval_seconds: int = 300) -> MonitorTarget:
    product = Product(kaspi_product_id="TEST-LEASE-1", merchant_sku="LEASE-1", name="Lease product")
    supplier = Supplier(code="ozon-lease", name="Ozon Lease")
    session.add_all([product, supplier])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="OZON-LEASE-1",
        title="Supplier product",
        url="https://example.com/item/1",
    )
    session.add(supplier_product)
    session.flush()

    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
    )
    session.add(binding)
    session.flush()

    target = MonitorTarget(
        product_binding_id=binding.id,
        status=MonitorStatus.ACTIVE.value,
        interval_seconds=interval_seconds,
        next_check_at=next_check_at,
    )
    session.add(target)
    session.commit()
    return target


def _without_tz(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def test_due_target_query_uses_postgresql_skip_locked() -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    statement = due_target_statement(now=now, limit=10)
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "NEXT_CHECK_AT" in sql
    assert "LEASE_UNTIL" in sql


def test_completion_query_locks_current_lease_row() -> None:
    statement = leased_target_statement(target_id=1, lease_token="token")
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "FOR UPDATE" in sql
    assert "LEASE_TOKEN" in sql


def test_one_worker_claims_due_target_and_second_worker_gets_none(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target = _seed_target(db_session, next_check_at=now - timedelta(minutes=1))

    first = claim_due_targets(db_session, lease_owner="worker-a", now=now, lease_seconds=120)
    second = claim_due_targets(db_session, lease_owner="worker-b", now=now, lease_seconds=120)

    assert len(first) == 1
    assert first[0].target_id == target.id
    assert first[0].lease_owner == "worker-a"
    assert len(first[0].lease_token) <= 64
    assert second == []


def test_expired_lease_can_be_reclaimed_with_new_token(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target = _seed_target(db_session, next_check_at=now - timedelta(minutes=1))

    first = claim_due_targets(db_session, lease_owner="worker-a", now=now, lease_seconds=30)[0]
    second = claim_due_targets(
        db_session,
        lease_owner="worker-b",
        now=now + timedelta(seconds=31),
        lease_seconds=60,
    )[0]

    assert second.target_id == target.id
    assert second.lease_owner == "worker-b"
    assert second.lease_token != first.lease_token


def test_stale_worker_cannot_release_or_reschedule_reclaimed_target(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target = _seed_target(db_session, next_check_at=now - timedelta(minutes=1))

    stale = claim_due_targets(db_session, lease_owner="worker-a", now=now, lease_seconds=30)[0]
    current = claim_due_targets(
        db_session,
        lease_owner="worker-b",
        now=now + timedelta(seconds=31),
        lease_seconds=60,
    )[0]

    assert release_target(db_session, target_id=target.id, lease_token=stale.lease_token) is False
    assert reschedule_success(
        db_session,
        target_id=target.id,
        lease_token=stale.lease_token,
        checked_at=now + timedelta(seconds=35),
    ) is False

    db_session.refresh(target)
    assert target.lease_token == current.lease_token
    assert target.lease_owner == "worker-b"


def test_success_reschedule_clears_lease_and_resets_failures(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target = _seed_target(db_session, next_check_at=now - timedelta(minutes=1), interval_seconds=300)
    target.consecutive_failures = 3
    db_session.commit()

    claim = claim_due_targets(db_session, lease_owner="worker-a", now=now)[0]
    completed_at = now + timedelta(seconds=5)

    assert reschedule_success(
        db_session,
        target_id=target.id,
        lease_token=claim.lease_token,
        checked_at=completed_at,
    ) is True

    db_session.refresh(target)
    assert target.lease_token is None
    assert target.lease_owner is None
    assert target.lease_until is None
    assert target.consecutive_failures == 0
    assert _without_tz(target.next_check_at) == _without_tz(completed_at + timedelta(seconds=300))


def test_failure_reschedule_uses_bounded_exponential_backoff(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target = _seed_target(db_session, next_check_at=now - timedelta(minutes=1), interval_seconds=300)
    claim = claim_due_targets(db_session, lease_owner="worker-a", now=now)[0]
    completed_at = now + timedelta(seconds=5)

    assert reschedule_failure(
        db_session,
        target_id=target.id,
        lease_token=claim.lease_token,
        checked_at=completed_at,
    ) is True

    db_session.refresh(target)
    assert target.consecutive_failures == 1
    assert target.lease_token is None
    assert _without_tz(target.next_check_at) == _without_tz(completed_at + timedelta(seconds=600))
