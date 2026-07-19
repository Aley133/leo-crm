from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .monitoring import AttemptOutcome, SourceHealth, SourceHealthStatus
from .supplier_adapters.base import AccessStrategy
from .suppliers import Supplier


_HARD_SIGNAL_POLICY: dict[AttemptOutcome, tuple[SourceHealthStatus, timedelta]] = {
    AttemptOutcome.RATE_LIMITED: (SourceHealthStatus.RATE_LIMITED, timedelta(minutes=15)),
    AttemptOutcome.CAPTCHA: (SourceHealthStatus.CAPTCHA_REQUIRED, timedelta(hours=1)),
    AttemptOutcome.BLOCKED: (SourceHealthStatus.BLOCKED, timedelta(hours=6)),
    AttemptOutcome.AUTH_REQUIRED: (SourceHealthStatus.AUTH_REQUIRED, timedelta(hours=1)),
}


def strategy_value(strategy: AccessStrategy | str) -> str:
    value = strategy.value if isinstance(strategy, AccessStrategy) else strategy
    value = value.strip()
    if not value:
        raise ValueError("access_strategy must not be empty")
    return value


def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps for consistent SQLite/PostgreSQL comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _lock_supplier(session: Session, supplier_id: int) -> Supplier:
    supplier = session.scalar(
        select(Supplier).where(Supplier.id == supplier_id).with_for_update()
    )
    if supplier is None:
        raise LookupError(f"Supplier {supplier_id} not found")
    return supplier


def get_source_health(
    session: Session,
    *,
    supplier_id: int,
    access_strategy: AccessStrategy | str = AccessStrategy.DIRECT_HTTP,
    for_update: bool = False,
) -> SourceHealth | None:
    statement = select(SourceHealth).where(
        SourceHealth.supplier_id == supplier_id,
        SourceHealth.access_strategy == strategy_value(access_strategy),
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _get_or_create_health(
    session: Session,
    *,
    supplier_id: int,
    access_strategy: AccessStrategy | str,
) -> SourceHealth:
    _lock_supplier(session, supplier_id)
    strategy = strategy_value(access_strategy)
    health = get_source_health(
        session,
        supplier_id=supplier_id,
        access_strategy=strategy,
        for_update=True,
    )
    if health is None:
        health = SourceHealth(supplier_id=supplier_id, access_strategy=strategy)
        session.add(health)
        session.flush()
    return health


def apply_source_success(
    session: Session,
    *,
    supplier_id: int,
    occurred_at: datetime,
    access_strategy: AccessStrategy | str = AccessStrategy.DIRECT_HTTP,
) -> SourceHealth:
    """Apply a successful supplier result without committing.

    ``direct_http`` remains the compatibility default for older internal callers.
    Runtime orchestration must pass the adapter strategy explicitly.
    """
    health = _get_or_create_health(
        session,
        supplier_id=supplier_id,
        access_strategy=access_strategy,
    )
    health.status = SourceHealthStatus.HEALTHY.value
    health.consecutive_failures = 0
    health.blocked_until = None
    health.last_success_at = occurred_at
    health.last_error_code = None
    session.flush()
    return health


def apply_source_failure(
    session: Session,
    *,
    supplier_id: int,
    outcome: AttemptOutcome,
    error_code: str,
    occurred_at: datetime,
    access_strategy: AccessStrategy | str = AccessStrategy.DIRECT_HTTP,
) -> SourceHealth:
    """Apply supplier evidence without committing.

    Infrastructure/persistence failures must never call this function. Hard
    signals open a bounded strategy-scoped breaker immediately. Ordinary
    failures degrade the strategy after three consecutive failed observations.
    ``direct_http`` is retained only as a compatibility default; orchestrators
    must provide the actual adapter strategy.
    """
    if outcome is AttemptOutcome.SUCCESS:
        raise ValueError("source failure cannot use success outcome")

    health = _get_or_create_health(
        session,
        supplier_id=supplier_id,
        access_strategy=access_strategy,
    )
    health.consecutive_failures += 1
    health.last_failure_at = occurred_at
    health.last_error_code = error_code

    hard_signal = _HARD_SIGNAL_POLICY.get(outcome)
    if hard_signal is not None:
        status, duration = hard_signal
        health.status = status.value
        health.blocked_until = occurred_at + duration
    elif health.consecutive_failures >= 3:
        health.status = SourceHealthStatus.DEGRADED.value
        health.blocked_until = None

    session.flush()
    return health


def source_is_blocked(health: SourceHealth | None, *, now: datetime) -> bool:
    if health is None or health.blocked_until is None:
        return False
    return _as_utc(health.blocked_until) > _as_utc(now)
