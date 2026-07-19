from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .lease_engine import (
    LeaseClaim,
    apply_failure_reschedule,
    apply_success_reschedule,
    claim_due_targets,
    utc_now,
)
from .monitoring import AttemptOutcome, MonitorTarget
from .observation_engine import (
    StaleLeaseError,
    persist_failed_attempt,
    persist_successful_observation,
)
from .source_health_engine import (
    apply_source_failure,
    apply_source_success,
    get_source_health,
    source_is_blocked,
    strategy_value,
)
from .supplier_adapters.base import AccessStrategy, AdapterRequest, SupplierAdapter
from .supplier_adapters.errors import AdapterError
from .suppliers import ProductBinding, Supplier, SupplierProduct


SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class MonitorTaskContext:
    monitor_target_id: int
    supplier_id: int
    supplier_product_id: int
    supplier_code: str
    external_id: str
    url: str


@dataclass(frozen=True, slots=True)
class ScheduledTaskResult:
    target_id: int
    status: str
    outcome: AttemptOutcome | None
    changed: bool | None = None
    error: str | None = None


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, SupplierAdapter] | None = None) -> None:
        self._adapters: dict[str, SupplierAdapter] = dict(adapters or {})

    def register(self, supplier_code: str, adapter: SupplierAdapter) -> None:
        code = supplier_code.strip().lower()
        if not code:
            raise ValueError("supplier_code must not be empty")
        self._adapters[code] = adapter

    def get(self, supplier_code: str) -> SupplierAdapter | None:
        return self._adapters.get(supplier_code.strip().lower())


def load_task_context(session: Session, target_id: int) -> MonitorTaskContext:
    row = session.execute(
        select(
            MonitorTarget.id,
            Supplier.id,
            SupplierProduct.id,
            Supplier.code,
            SupplierProduct.external_id,
            SupplierProduct.url,
        )
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(MonitorTarget.id == target_id)
    ).one_or_none()
    if row is None:
        raise LookupError(f"MonitorTarget {target_id} context not found")
    return MonitorTaskContext(
        monitor_target_id=row[0],
        supplier_id=row[1],
        supplier_product_id=row[2],
        supplier_code=row[3],
        external_id=row[4],
        url=row[5],
    )


def classify_adapter_exception(exc: Exception) -> tuple[AttemptOutcome, str, int | None]:
    if isinstance(exc, AdapterError):
        return exc.outcome, exc.error_code, exc.http_status
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return AttemptOutcome.TIMEOUT, "adapter_timeout", None
    if isinstance(exc, ConnectionError):
        return AttemptOutcome.NETWORK_ERROR, "adapter_network_error", None
    if isinstance(exc, ValueError):
        return AttemptOutcome.PARSE_ERROR, "adapter_invalid_response", None
    return AttemptOutcome.INTERNAL_ERROR, "adapter_internal_error", None


def _persist_failure_and_reschedule(
    session: Session,
    *,
    claim: LeaseClaim,
    adapter_code: str,
    access_strategy: AccessStrategy | str,
    started_at: datetime,
    finished_at: datetime,
    outcome: AttemptOutcome,
    error_code: str,
    error_message: str,
    supplier_id: int | None = None,
    http_status: int | None = None,
) -> None:
    strategy = strategy_value(access_strategy)
    persist_failed_attempt(
        session,
        monitor_target_id=claim.target_id,
        lease_token=claim.lease_token,
        adapter_code=adapter_code,
        access_strategy=strategy,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
        http_status=http_status,
        error_code=error_code,
        error_message=error_message,
    )
    if supplier_id is not None:
        apply_source_failure(
            session,
            supplier_id=supplier_id,
            access_strategy=strategy,
            outcome=outcome,
            error_code=error_code,
            occurred_at=finished_at,
        )
    completed = apply_failure_reschedule(
        session,
        target_id=claim.target_id,
        lease_token=claim.lease_token,
        checked_at=finished_at,
    )
    if not completed:
        raise StaleLeaseError(f"MonitorTarget {claim.target_id} lease changed before failure completion")


def _commit_failure_result(
    *,
    session_factory: SessionFactory,
    claim: LeaseClaim,
    adapter_code: str,
    access_strategy: AccessStrategy | str,
    started_at: datetime,
    finished_at: datetime,
    outcome: AttemptOutcome,
    error_code: str,
    error_message: str,
    supplier_id: int | None = None,
    http_status: int | None = None,
) -> None:
    with session_factory() as session:
        try:
            _persist_failure_and_reschedule(
                session,
                claim=claim,
                adapter_code=adapter_code,
                access_strategy=access_strategy,
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                error_code=error_code,
                error_message=error_message,
                supplier_id=supplier_id,
                http_status=http_status,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


def _defer_blocked_target(
    session: Session,
    *,
    claim: LeaseClaim,
    blocked_until: datetime,
) -> None:
    target = session.scalar(
        select(MonitorTarget)
        .where(
            MonitorTarget.id == claim.target_id,
            MonitorTarget.lease_token == claim.lease_token,
        )
        .with_for_update()
    )
    if target is None:
        raise StaleLeaseError(f"MonitorTarget {claim.target_id} lease changed before breaker deferral")
    target.next_check_at = blocked_until
    target.lease_owner = None
    target.lease_token = None
    target.lease_until = None
    session.flush()


async def process_claimed_target(
    claim: LeaseClaim,
    *,
    registry: AdapterRegistry,
    session_factory: SessionFactory = SessionLocal,
    now_factory: Callable[[], datetime] = utc_now,
) -> ScheduledTaskResult:
    try:
        with session_factory() as session:
            context = load_task_context(session, claim.target_id)
    except Exception as exc:
        return ScheduledTaskResult(
            target_id=claim.target_id,
            status="context_error",
            outcome=AttemptOutcome.INTERNAL_ERROR,
            error=str(exc),
        )

    adapter = registry.get(context.supplier_code)
    started_at = now_factory()

    if adapter is None:
        outcome = AttemptOutcome.INTERNAL_ERROR
        error_code = "adapter_not_registered"
        error_message = f"No adapter registered for supplier {context.supplier_code}"
        finished_at = now_factory()
        try:
            _commit_failure_result(
                session_factory=session_factory,
                claim=claim,
                adapter_code=context.supplier_code,
                access_strategy=AccessStrategy.REGISTRY,
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                error_code=error_code,
                error_message=error_message,
                supplier_id=None,
            )
        except StaleLeaseError:
            return ScheduledTaskResult(claim.target_id, "stale", outcome, error=error_message)
        except Exception as exc:
            return ScheduledTaskResult(claim.target_id, "persistence_error", outcome, error=str(exc))
        return ScheduledTaskResult(claim.target_id, "failed", outcome, error=error_message)

    strategy = strategy_value(adapter.access_strategy)
    try:
        with session_factory() as session:
            health = get_source_health(
                session,
                supplier_id=context.supplier_id,
                access_strategy=strategy,
            )
            if source_is_blocked(health, now=started_at):
                assert health is not None and health.blocked_until is not None
                _defer_blocked_target(
                    session,
                    claim=claim,
                    blocked_until=health.blocked_until,
                )
                session.commit()
                return ScheduledTaskResult(
                    target_id=claim.target_id,
                    status="source_blocked",
                    outcome=None,
                    error=f"{context.supplier_code}/{strategy} blocked until {health.blocked_until.isoformat()}",
                )
    except StaleLeaseError:
        return ScheduledTaskResult(claim.target_id, "stale", None)
    except Exception as exc:
        return ScheduledTaskResult(claim.target_id, "persistence_error", None, error=str(exc))

    request = AdapterRequest(
        supplier_product_id=context.supplier_product_id,
        url=context.url,
        external_id=context.external_id,
    )

    try:
        offer = await adapter.fetch(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome, error_code, http_status = classify_adapter_exception(exc)
        finished_at = now_factory()
        try:
            _commit_failure_result(
                session_factory=session_factory,
                claim=claim,
                adapter_code=adapter.code,
                access_strategy=strategy,
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                error_code=error_code,
                error_message=str(exc),
                supplier_id=context.supplier_id,
                http_status=http_status,
            )
        except StaleLeaseError:
            return ScheduledTaskResult(claim.target_id, "stale", outcome, error=str(exc))
        except Exception as persistence_exc:
            return ScheduledTaskResult(claim.target_id, "persistence_error", outcome, error=str(persistence_exc))
        return ScheduledTaskResult(
            target_id=claim.target_id,
            status="failed",
            outcome=outcome,
            error=str(exc),
        )

    finished_at = now_factory()
    try:
        with session_factory() as session:
            try:
                observation = persist_successful_observation(
                    session,
                    monitor_target_id=claim.target_id,
                    lease_token=claim.lease_token,
                    adapter_code=adapter.code,
                    access_strategy=strategy,
                    started_at=started_at,
                    finished_at=finished_at,
                    offer=offer,
                )
                apply_source_success(
                    session,
                    supplier_id=context.supplier_id,
                    access_strategy=strategy,
                    occurred_at=finished_at,
                )
                completed = apply_success_reschedule(
                    session,
                    target_id=claim.target_id,
                    lease_token=claim.lease_token,
                    checked_at=finished_at,
                )
                if not completed:
                    raise StaleLeaseError(
                        f"MonitorTarget {claim.target_id} lease changed before success completion"
                    )
                session.commit()
            except Exception:
                session.rollback()
                raise
    except StaleLeaseError:
        return ScheduledTaskResult(claim.target_id, "stale", None)
    except Exception as exc:
        return ScheduledTaskResult(
            target_id=claim.target_id,
            status="persistence_error",
            outcome=None,
            error=str(exc),
        )

    return ScheduledTaskResult(
        target_id=claim.target_id,
        status="succeeded",
        outcome=AttemptOutcome.SUCCESS,
        changed=observation.changed,
    )


async def run_scheduler_tick(
    *,
    worker_id: str,
    registry: AdapterRegistry,
    session_factory: SessionFactory = SessionLocal,
    batch_size: int = 10,
    concurrency: int = 4,
    lease_seconds: int = 120,
    shard: int | None = None,
    now: datetime | None = None,
    now_factory: Callable[[], datetime] = utc_now,
) -> list[ScheduledTaskResult]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    claimed_at = now or now_factory()
    with session_factory() as session:
        claims = claim_due_targets(
            session,
            lease_owner=worker_id,
            limit=batch_size,
            lease_seconds=lease_seconds,
            now=claimed_at,
            shard=shard,
        )

    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(claim: LeaseClaim) -> ScheduledTaskResult:
        async with semaphore:
            return await process_claimed_target(
                claim,
                registry=registry,
                session_factory=session_factory,
                now_factory=now_factory,
            )

    tasks = [asyncio.create_task(guarded(claim)) for claim in claims]
    if not tasks:
        return []
    try:
        return list(await asyncio.gather(*tasks))
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
