from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .lease_engine import LeaseClaim, claim_due_targets, reschedule_failure, reschedule_success, utc_now
from .monitoring import AttemptOutcome, MonitorTarget
from .observation_engine import StaleLeaseError, record_failed_attempt, record_successful_observation
from .supplier_adapters.base import AdapterRequest, SupplierAdapter
from .supplier_adapters.errors import AdapterError
from .suppliers import ProductBinding, Supplier, SupplierProduct


SessionFactory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class MonitorTaskContext:
    monitor_target_id: int
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
        supplier_product_id=row[1],
        supplier_code=row[2],
        external_id=row[3],
        url=row[4],
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


def _commit_failure(
    session: Session,
    *,
    claim: LeaseClaim,
    adapter_code: str,
    access_strategy: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: AttemptOutcome,
    error_code: str,
    error_message: str,
    http_status: int | None = None,
) -> bool:
    """Persist failure and reschedule in one transaction."""
    try:
        record_failed_attempt(
            session,
            monitor_target_id=claim.target_id,
            lease_token=claim.lease_token,
            adapter_code=adapter_code,
            access_strategy=access_strategy,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            http_status=http_status,
            error_code=error_code,
            error_message=error_message,
            commit=False,
        )
        completed = reschedule_failure(
            session,
            target_id=claim.target_id,
            lease_token=claim.lease_token,
            checked_at=finished_at,
            commit=False,
        )
        if not completed:
            session.rollback()
            return False
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


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
        try:
            with session_factory() as session:
                completed = _commit_failure(
                    session,
                    claim=claim,
                    adapter_code=context.supplier_code,
                    access_strategy="registry",
                    started_at=started_at,
                    finished_at=now_factory(),
                    outcome=outcome,
                    error_code=error_code,
                    error_message=error_message,
                )
        except StaleLeaseError:
            return ScheduledTaskResult(claim.target_id, "stale", outcome, error=error_message)
        return ScheduledTaskResult(
            claim.target_id,
            "failed" if completed else "stale",
            outcome,
            error=error_message,
        )

    request = AdapterRequest(
        supplier_product_id=context.supplier_product_id,
        url=context.url,
        external_id=context.external_id,
    )

    try:
        offer = await adapter.fetch(request)
        finished_at = now_factory()
        with session_factory() as session:
            try:
                observation = record_successful_observation(
                    session,
                    monitor_target_id=claim.target_id,
                    lease_token=claim.lease_token,
                    adapter_code=adapter.code,
                    access_strategy=adapter.access_strategy,
                    started_at=started_at,
                    finished_at=finished_at,
                    offer=offer,
                    commit=False,
                )
                completed = reschedule_success(
                    session,
                    target_id=claim.target_id,
                    lease_token=claim.lease_token,
                    checked_at=finished_at,
                    commit=False,
                )
                if not completed:
                    session.rollback()
                    return ScheduledTaskResult(claim.target_id, "stale", AttemptOutcome.SUCCESS)
                session.commit()
            except Exception:
                session.rollback()
                raise
        return ScheduledTaskResult(
            target_id=claim.target_id,
            status="succeeded",
            outcome=AttemptOutcome.SUCCESS,
            changed=observation.changed,
        )
    except StaleLeaseError:
        return ScheduledTaskResult(claim.target_id, "stale", None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome, error_code, http_status = classify_adapter_exception(exc)
        finished_at = now_factory()
        try:
            with session_factory() as session:
                completed = _commit_failure(
                    session,
                    claim=claim,
                    adapter_code=adapter.code,
                    access_strategy=adapter.access_strategy,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome=outcome,
                    http_status=http_status,
                    error_code=error_code,
                    error_message=str(exc),
                )
            return ScheduledTaskResult(
                target_id=claim.target_id,
                status="failed" if completed else "stale",
                outcome=outcome,
                error=str(exc),
            )
        except StaleLeaseError:
            return ScheduledTaskResult(claim.target_id, "stale", outcome, error=str(exc))


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
