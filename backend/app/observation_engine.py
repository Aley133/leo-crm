from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .monitoring import (
    AttemptOutcome,
    MonitorAttempt,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from .supplier_adapters.base import NormalizedOffer
from .suppliers import ProductBinding


class StaleLeaseError(RuntimeError):
    """Raised when a worker no longer owns the target lease."""


@dataclass(frozen=True, slots=True)
class ObservationResult:
    attempt_id: int
    supplier_product_id: int
    state_version: int
    observation_id: int | None
    changed: bool
    fingerprint: str


def _lock_current_target(
    session: Session,
    *,
    monitor_target_id: int,
    lease_token: str,
) -> MonitorTarget:
    """Lock the target row and verify that the caller still owns its lease."""
    target = session.scalar(
        select(MonitorTarget)
        .where(
            MonitorTarget.id == monitor_target_id,
            MonitorTarget.lease_token == lease_token,
        )
        .with_for_update()
    )
    if target is None:
        raise StaleLeaseError(
            f"MonitorTarget {monitor_target_id} is not owned by the supplied lease token"
        )
    return target


def _supplier_product_id_for_target(session: Session, target: MonitorTarget) -> int:
    supplier_product_id = session.scalar(
        select(ProductBinding.supplier_product_id).where(
            ProductBinding.id == target.product_binding_id
        )
    )
    if supplier_product_id is None:
        raise LookupError(f"MonitorTarget {target.id} is not linked to a supplier product")
    return supplier_product_id


def _normalized_metadata(offer: NormalizedOffer) -> str | None:
    if not offer.raw_metadata:
        return None
    return json.dumps(offer.raw_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_successful_observation(
    session: Session,
    *,
    monitor_target_id: int,
    lease_token: str,
    adapter_code: str,
    access_strategy: str,
    started_at: datetime,
    finished_at: datetime,
    offer: NormalizedOffer,
    http_status: int | None = 200,
    commit: bool = True,
) -> ObservationResult:
    """Persist a successful adapter result.

    A new append-only observation is created whenever the current state changes,
    including a return to a previously seen fingerprint (A -> B -> A).
    ``commit=False`` lets the scheduler own the transaction and atomically pair
    this write with rescheduling and lease release.
    """
    if not lease_token.strip():
        raise ValueError("lease_token must not be empty")
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")

    try:
        target = _lock_current_target(
            session,
            monitor_target_id=monitor_target_id,
            lease_token=lease_token,
        )
        supplier_product_id = _supplier_product_id_for_target(session, target)
        if offer.supplier_product_id != supplier_product_id:
            raise ValueError("offer supplier_product_id does not match monitor target")

        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        fingerprint = offer.fingerprint

        attempt = MonitorAttempt(
            monitor_target_id=monitor_target_id,
            lease_token=lease_token,
            outcome=AttemptOutcome.SUCCESS.value,
            adapter_code=adapter_code,
            access_strategy=access_strategy,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            http_status=http_status,
        )
        session.add(attempt)
        session.flush()

        state = session.scalar(
            select(SupplierOfferState)
            .where(SupplierOfferState.supplier_product_id == supplier_product_id)
            .with_for_update()
        )

        changed = state is None or state.fingerprint != fingerprint
        observation_id: int | None = None

        if state is None:
            state = SupplierOfferState(
                supplier_product_id=supplier_product_id,
                price=offer.price,
                old_price=offer.old_price,
                available=offer.available,
                stock=offer.stock,
                delivery_days=offer.delivery_days,
                seller=offer.seller,
                fingerprint=fingerprint,
                adapter_schema_version=offer.adapter_schema_version,
                observed_at=offer.observed_at,
                last_checked_at=finished_at,
                version=1,
            )
            session.add(state)
            session.flush()
        elif changed:
            state.price = offer.price
            state.old_price = offer.old_price
            state.available = offer.available
            state.stock = offer.stock
            state.delivery_days = offer.delivery_days
            state.seller = offer.seller
            state.fingerprint = fingerprint
            state.adapter_schema_version = offer.adapter_schema_version
            state.observed_at = offer.observed_at
            state.last_checked_at = finished_at
            state.version += 1
            session.flush()
        else:
            state.last_checked_at = finished_at
            session.flush()

        if changed:
            observation = SupplierOfferObservation(
                supplier_product_id=supplier_product_id,
                monitor_attempt_id=attempt.id,
                price=offer.price,
                old_price=offer.old_price,
                available=offer.available,
                stock=offer.stock,
                delivery_days=offer.delivery_days,
                seller=offer.seller,
                fingerprint=fingerprint,
                adapter_schema_version=offer.adapter_schema_version,
                raw_metadata=_normalized_metadata(offer),
                observed_at=offer.observed_at,
            )
            session.add(observation)
            session.flush()
            observation_id = observation.id

        if commit:
            session.commit()
        return ObservationResult(
            attempt_id=attempt.id,
            supplier_product_id=supplier_product_id,
            state_version=state.version,
            observation_id=observation_id,
            changed=changed,
            fingerprint=fingerprint,
        )
    except Exception:
        if commit:
            session.rollback()
        raise


def record_failed_attempt(
    session: Session,
    *,
    monitor_target_id: int,
    lease_token: str,
    adapter_code: str,
    access_strategy: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: AttemptOutcome,
    http_status: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    commit: bool = True,
) -> int:
    """Record a failed adapter attempt without mutating offer state."""
    if not lease_token.strip():
        raise ValueError("lease_token must not be empty")
    if outcome is AttemptOutcome.SUCCESS:
        raise ValueError("record_failed_attempt cannot use success outcome")
    if finished_at < started_at:
        raise ValueError("finished_at must not precede started_at")

    duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    attempt = MonitorAttempt(
        monitor_target_id=monitor_target_id,
        lease_token=lease_token,
        outcome=outcome.value,
        adapter_code=adapter_code,
        access_strategy=access_strategy,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        http_status=http_status,
        error_code=error_code,
        error_message=error_message,
    )
    try:
        _lock_current_target(
            session,
            monitor_target_id=monitor_target_id,
            lease_token=lease_token,
        )
        session.add(attempt)
        session.flush()
        if commit:
            session.commit()
        return attempt.id
    except Exception:
        if commit:
            session.rollback()
        raise
