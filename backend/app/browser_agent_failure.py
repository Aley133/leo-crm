from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .browser_agent_models import BrowserAgentJob
from .monitoring import AttemptOutcome, MonitorAttempt, MonitorTarget
from .source_health_engine import apply_source_failure
from .supplier_adapters.base import AccessStrategy
from .suppliers import ProductBinding, Supplier, SupplierProduct


def _failure_outcome(error_code: str | None) -> AttemptOutcome:
    normalized = (error_code or "").casefold()
    if "captcha" in normalized:
        return AttemptOutcome.CAPTCHA
    if "blocked" in normalized:
        return AttemptOutcome.BLOCKED
    if "timeout" in normalized:
        return AttemptOutcome.TIMEOUT
    if "network" in normalized or "pool" in normalized or "connection" in normalized:
        return AttemptOutcome.NETWORK_ERROR
    if "parse" in normalized:
        return AttemptOutcome.PARSE_ERROR
    return AttemptOutcome.INTERNAL_ERROR


def persist_browser_agent_failure(
    session: Session,
    *,
    job: BrowserAgentJob,
    error_code: str | None,
    error_message: str | None,
    finished_at: datetime,
) -> int | None:
    """Record one failed local-browser check and reschedule only its target."""
    if job.monitor_target_id is None:
        return None

    target = session.scalar(
        select(MonitorTarget)
        .where(MonitorTarget.id == job.monitor_target_id)
        .with_for_update()
    )
    if target is None:
        return None

    row = session.execute(
        select(Supplier.id)
        .join(SupplierProduct, SupplierProduct.supplier_id == Supplier.id)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .where(
            ProductBinding.id == target.product_binding_id,
            SupplierProduct.id == job.supplier_product_id,
        )
    ).one_or_none()
    supplier_id = row[0] if row is not None else None

    started_at = job.created_at
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=finished_at.tzinfo)

    outcome = _failure_outcome(error_code)
    attempt = MonitorAttempt(
        monitor_target_id=target.id,
        lease_token=f"browser-agent:{job.id}",
        outcome=outcome.value,
        adapter_code="ozon-browser-agent-v1",
        access_strategy=AccessStrategy.BROWSER.value,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
        error_code=(error_code or "browser_agent_error")[:128],
        error_message=(error_message or "Browser agent check failed")[:4000],
    )
    session.add(attempt)
    session.flush()

    target.last_checked_at = finished_at
    target.consecutive_failures += 1
    base_delay = max(60, target.interval_seconds)
    multiplier = 2 ** min(target.consecutive_failures - 1, 5)
    target.next_check_at = finished_at + timedelta(seconds=min(base_delay * multiplier, 21_600))

    if supplier_id is not None:
        apply_source_failure(
            session,
            supplier_id=supplier_id,
            access_strategy=AccessStrategy.BROWSER.value,
            outcome=outcome,
            error_code=attempt.error_code,
            occurred_at=finished_at,
        )
    session.flush()
    return attempt.id
