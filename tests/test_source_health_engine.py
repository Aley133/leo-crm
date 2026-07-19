from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.monitoring import AttemptOutcome, SourceHealth, SourceHealthStatus
from backend.app.source_health_engine import apply_source_failure, apply_source_success, source_is_blocked
from backend.app.supplier_adapters.base import AccessStrategy
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
        access_strategy=AccessStrategy.DIRECT_HTTP,
        outcome=AttemptOutcome.CAPTCHA,
        error_code="captcha_detected",
        occurred_at=now,
    )
    db_session.commit()

    assert health.access_strategy == AccessStrategy.DIRECT_HTTP.value
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
            access_strategy=AccessStrategy.DIRECT_HTTP,
            outcome=AttemptOutcome.NETWORK_ERROR,
            error_code="network_error",
            occurred_at=now + timedelta(minutes=index),
        )
        db_session.commit()

    assert health.status == SourceHealthStatus.DEGRADED.value
    assert health.consecutive_failures == 3
    assert health.blocked_until is None


def test_success_recovers_only_matching_strategy(db_session: Session) -> None:
    supplier = _supplier(db_session)
    failed_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    succeeded_at = failed_at + timedelta(minutes=5)

    direct = apply_source_failure(
        db_session,
        supplier_id=supplier.id,
        access_strategy=AccessStrategy.DIRECT_HTTP,
        outcome=AttemptOutcome.BLOCKED,
        error_code="http_403_blocked",
        occurred_at=failed_at,
    )
    browser = apply_source_failure(
        db_session,
        supplier_id=supplier.id,
        access_strategy=AccessStrategy.BROWSER,
        outcome=AttemptOutcome.CAPTCHA,
        error_code="browser_captcha",
        occurred_at=failed_at,
    )
    db_session.commit()

    recovered = apply_source_success(
        db_session,
        supplier_id=supplier.id,
        access_strategy=AccessStrategy.DIRECT_HTTP,
        occurred_at=succeeded_at,
    )
    db_session.commit()

    db_session.refresh(browser)
    assert recovered.id == direct.id
    assert recovered.status == SourceHealthStatus.HEALTHY.value
    assert recovered.consecutive_failures == 0
    assert recovered.blocked_until is None
    assert recovered.last_success_at.replace(tzinfo=UTC) == succeeded_at
    assert recovered.last_error_code is None
    assert browser.status == SourceHealthStatus.CAPTCHA_REQUIRED.value
    assert browser.blocked_until is not None
    assert db_session.scalar(select(func.count()).select_from(SourceHealth)) == 2
