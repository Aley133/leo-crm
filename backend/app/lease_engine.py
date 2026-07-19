from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, or_, select, update
from sqlalchemy.orm import Session

from .monitoring import MonitorStatus, MonitorTarget


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    target_id: int
    product_binding_id: int
    lease_owner: str
    lease_token: str
    lease_until: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def _new_lease_token() -> str:
    return secrets.token_urlsafe(32)


def due_target_statement(
    *,
    now: datetime,
    limit: int,
    shard: int | None = None,
) -> Select[tuple[MonitorTarget]]:
    statement = (
        select(MonitorTarget)
        .where(
            MonitorTarget.status == MonitorStatus.ACTIVE.value,
            MonitorTarget.next_check_at <= now,
            or_(MonitorTarget.lease_until.is_(None), MonitorTarget.lease_until < now),
        )
        .order_by(MonitorTarget.next_check_at.asc(), MonitorTarget.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if shard is not None:
        statement = statement.where(MonitorTarget.shard == shard)
    return statement


def leased_target_statement(*, target_id: int, lease_token: str) -> Select[tuple[MonitorTarget]]:
    return (
        select(MonitorTarget)
        .where(
            MonitorTarget.id == target_id,
            MonitorTarget.lease_token == lease_token,
        )
        .with_for_update()
    )


def claim_due_targets(
    session: Session,
    *,
    lease_owner: str,
    limit: int = 10,
    lease_seconds: int = 120,
    now: datetime | None = None,
    shard: int | None = None,
) -> list[LeaseClaim]:
    """Atomically claim due targets and commit the short claim transaction."""
    if not lease_owner.strip():
        raise ValueError("lease_owner must not be empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    if shard is not None and not 0 <= shard < 100:
        raise ValueError("shard must be between 0 and 99")

    claimed_at = now or utc_now()
    lease_until = claimed_at + timedelta(seconds=lease_seconds)
    statement = due_target_statement(now=claimed_at, limit=limit, shard=shard)

    try:
        targets = list(session.scalars(statement).all())
        claims: list[LeaseClaim] = []
        for target in targets:
            token = _new_lease_token()
            target.lease_owner = lease_owner
            target.lease_token = token
            target.lease_until = lease_until
            claims.append(
                LeaseClaim(
                    target_id=target.id,
                    product_binding_id=target.product_binding_id,
                    lease_owner=lease_owner,
                    lease_token=token,
                    lease_until=lease_until,
                )
            )
        session.commit()
        return claims
    except Exception:
        session.rollback()
        raise


def release_target(
    session: Session,
    *,
    target_id: int,
    lease_token: str,
) -> bool:
    statement = (
        update(MonitorTarget)
        .where(MonitorTarget.id == target_id, MonitorTarget.lease_token == lease_token)
        .values(lease_owner=None, lease_token=None, lease_until=None)
    )
    try:
        result = session.execute(statement)
        session.commit()
        return result.rowcount == 1
    except Exception:
        session.rollback()
        raise


def apply_success_reschedule(
    session: Session,
    *,
    target_id: int,
    lease_token: str,
    checked_at: datetime | None = None,
) -> bool:
    """Apply success scheduling without owning the transaction."""
    completed_at = checked_at or utc_now()
    target = session.scalar(leased_target_statement(target_id=target_id, lease_token=lease_token))
    if target is None:
        return False

    target.last_checked_at = completed_at
    target.consecutive_failures = 0
    target.next_check_at = completed_at + timedelta(seconds=target.interval_seconds)
    target.lease_owner = None
    target.lease_token = None
    target.lease_until = None
    session.flush()
    return True


def reschedule_success(
    session: Session,
    *,
    target_id: int,
    lease_token: str,
    checked_at: datetime | None = None,
) -> bool:
    """Backward-compatible transaction-owning success wrapper."""
    try:
        completed = apply_success_reschedule(
            session,
            target_id=target_id,
            lease_token=lease_token,
            checked_at=checked_at,
        )
        if completed:
            session.commit()
        else:
            session.rollback()
        return completed
    except Exception:
        session.rollback()
        raise


def apply_failure_reschedule(
    session: Session,
    *,
    target_id: int,
    lease_token: str,
    checked_at: datetime | None = None,
    max_backoff_seconds: int = 86_400,
) -> bool:
    """Apply bounded failure backoff without owning the transaction."""
    if max_backoff_seconds < 60:
        raise ValueError("max_backoff_seconds must be at least 60")

    completed_at = checked_at or utc_now()
    target = session.scalar(leased_target_statement(target_id=target_id, lease_token=lease_token))
    if target is None:
        return False

    failures = target.consecutive_failures + 1
    multiplier = 2 ** min(failures, 16)
    backoff_seconds = min(target.interval_seconds * multiplier, max_backoff_seconds)

    target.last_checked_at = completed_at
    target.consecutive_failures = failures
    target.next_check_at = completed_at + timedelta(seconds=backoff_seconds)
    target.lease_owner = None
    target.lease_token = None
    target.lease_until = None
    session.flush()
    return True


def reschedule_failure(
    session: Session,
    *,
    target_id: int,
    lease_token: str,
    checked_at: datetime | None = None,
    max_backoff_seconds: int = 86_400,
) -> bool:
    """Backward-compatible transaction-owning failure wrapper."""
    try:
        completed = apply_failure_reschedule(
            session,
            target_id=target_id,
            lease_token=lease_token,
            checked_at=checked_at,
            max_backoff_seconds=max_backoff_seconds,
        )
        if completed:
            session.commit()
        else:
            session.rollback()
        return completed
    except Exception:
        session.rollback()
        raise
