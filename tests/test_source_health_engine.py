from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from backend.app.monitoring import AttemptOutcome, SourceHealthStatus
from backend.app.source_health_engine import apply_source_failure, apply_source_success, source_is_blocked
from backend.app.suppliers import Supplier


def _supplier(session: Session) -> Supplier:
    supplier = Supplier(code="health-test", name="Health Test")
    session.add(supplier)
    session.commit()
    return supplier


def test_hard_signal_opens_bounded_source_breaker(db_session: Session) -> None:
    supplier = _supplier(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    health = apply_source_failure(
        db_session,
        supplier_id=supplier.id,
        outcome=AttemptOutcome.CAPTCHA,
        error_code="captcha_detected",
        occurred_at=now,
    )
    db_session.commit()

    assert health.status == SourceHealthStatus.CAPTCHA_REQUIRED.value
    assert health.consecutive_failures == 1
    assert health.blocked_until is not None
    assert health.blocked_until.replace(tzinfo=UTC) == now + timedelta(hours=1)
    assert source_is_blocked(health, now=now + timedelta(minutes=30)) is True
    assert source_is_blocked(health, now=now + timedelta(hours=2)) is False


def test_ordinary_failures_degrade_after_three_attempts(db_session: Session) -> None:
    supplier = _supplier(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    for index in range(3):
        health = apply_source_failure(
            db_session,
            supplier_id=supplier.id,
            outcome=AttemptOutcome.NETWORK_ERROR,
            error_code="network_error",
            occurred_at=now + timedelta(minutes=index),
        )
        db_session.commit()

    assert health.status == SourceHealthStatus.DEGRADED.value
    assert health.consecutive_failures == 3
    assert health.blocked_until is None


def test_success_recovers_source_health(db_session: Session) -> None:
    supplier = _supplier(db_session)
    failed_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    succeeded_at = failed_at + timedelta(minutes=5)

    apply_source_failure(
        db_session,
        supplier_id=supplier.id,
        outcome=AttemptOutcome.BLOCKED,
        error_code="http_403_blocked",
        occurred_at=failed_at,
    )
    db_session.commit()

    health = apply_source_success(
        db_session,
        supplier_id=supplier.id,
        occurred_at=succeeded_at,
    )
    db_session.commit()

    assert health.status == SourceHealthStatus.HEALTHY.value
    assert health.consecutive_failures == 0
    assert health.blocked_until is None
    assert health.last_success_at.replace(tzinfo=UTC) == succeeded_at
    assert health.last_error_code is None
