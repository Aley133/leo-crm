from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .monitoring import AttemptOutcome, SourceHealth, SourceHealthStatus


def _same_timezone(value: datetime | None, reference: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _locked_health(session: Session, supplier_id: int) -> SourceHealth:
    health = session.scalar(
        select(SourceHealth)
        .where(SourceHealth.supplier_id == supplier_id)
        .with_for_update()
    )
    if health is None:
        health = SourceHealth(supplier_id=supplier_id)
        session.add(health)
        session.flush()
    return health


def source_blocked_until(
    session: Session,
    *,
    supplier_id: int,
    now: datetime,
) -> datetime | None:
    health = session.scalar(
        select(SourceHealth).where(SourceHealth.supplier_id == supplier_id)
    )
    if health is None:
        return None
    blocked_until = _same_timezone(health.blocked_until, now)
    if blocked_until is None or blocked_until <= now:
        return None
    return blocked_until


def record_source_success(
    session: Session,
    *,
    supplier_id: int,
    finished_at: datetime,
) -> SourceHealth:
    health = _locked_health(session, supplier_id)
    health.status = SourceHealthStatus.HEALTHY.value
    health.consecutive_failures = 0
    health.blocked_until = None
    health.last_success_at = finished_at
    health.last_error_code = None
    session.flush()
    return health


def record_source_failure(
    session: Session,
    *,
    supplier_id: int,
    outcome: AttemptOutcome,
    error_code: str,
    finished_at: datetime,
) -> SourceHealth:
    health = _locked_health(session, supplier_id)
    failures = health.consecutive_failures + 1
    health.consecutive_failures = failures
    health.last_failure_at = finished_at
    health.last_error_code = error_code

    delay: timedelta | None
    if outcome is AttemptOutcome.RATE_LIMITED:
        health.status = SourceHealthStatus.RATE_LIMITED.value
        delay = timedelta(minutes=min(15 * (2 ** min(failures - 1, 3)), 120))
    elif outcome is AttemptOutcome.CAPTCHA:
        health.status = SourceHealthStatus.CAPTCHA_REQUIRED.value
        delay = timedelta(hours=min(2 ** min(failures - 1, 3), 8))
    elif outcome is AttemptOutcome.BLOCKED:
        health.status = SourceHealthStatus.BLOCKED.value
        delay = timedelta(hours=min(6 * (2 ** min(failures - 1, 2)), 24))
    elif outcome is AttemptOutcome.AUTH_REQUIRED:
        health.status = SourceHealthStatus.AUTH_REQUIRED.value
        delay = timedelta(hours=24)
    else:
        health.status = SourceHealthStatus.DEGRADED.value
        # One timeout or parser failure is target-local noise. Global source
        # suppression starts only after three consecutive transient failures.
        delay = None if failures < 3 else timedelta(
            minutes=min(5 * (2 ** min(failures - 3, 4)), 60)
        )

    if delay is not None:
        candidate = finished_at + delay
        current = _same_timezone(health.blocked_until, finished_at)
        if current is None or candidate > current:
            health.blocked_until = candidate
    session.flush()
    return health
