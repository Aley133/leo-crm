from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .browser_agent_models import BrowserAgentJob
from .monitoring import (
    AttemptOutcome,
    MonitorAttempt,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from .source_health_engine import apply_source_success
from .supplier_adapters.base import AccessStrategy, NormalizedOffer
from .suppliers import ProductBinding, Supplier, SupplierProduct


class BrowserAgentResultError(ValueError):
    pass


def _decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BrowserAgentResultError(f"{field} must be a decimal or null") from exc
    if parsed < 0:
        raise BrowserAgentResultError(f"{field} must not be negative")
    return parsed


def normalized_offer_from_agent(job: BrowserAgentJob, payload: dict[str, Any]) -> NormalizedOffer:
    observed_raw = payload.get("observed_at")
    if not isinstance(observed_raw, str) or not observed_raw.strip():
        raise BrowserAgentResultError("observed_at is required")
    try:
        observed_at = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrowserAgentResultError("observed_at must be ISO-8601") from exc
    if observed_at.tzinfo is None:
        raise BrowserAgentResultError("observed_at must be timezone-aware")

    schema_version = str(payload.get("adapter_schema_version") or "").strip()
    if not schema_version:
        raise BrowserAgentResultError("adapter_schema_version is required")

    raw_metadata = payload.get("raw_metadata")
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict):
        raise BrowserAgentResultError("raw_metadata must be an object")
    raw_metadata = dict(raw_metadata)
    raw_metadata["execution_surface"] = "local_browser_agent"
    raw_metadata["browser_agent_job_id"] = job.id

    return NormalizedOffer(
        supplier_product_id=job.supplier_product_id,
        price=_decimal(payload.get("price"), field="price"),
        old_price=_decimal(payload.get("old_price"), field="old_price"),
        available=payload.get("available"),
        stock=payload.get("stock"),
        delivery_days=payload.get("delivery_days"),
        seller=(str(payload["seller"]).strip() if payload.get("seller") not in (None, "") else None),
        adapter_schema_version=schema_version,
        observed_at=observed_at,
        raw_metadata=raw_metadata,
    )


def persist_browser_agent_success(
    session: Session,
    *,
    job: BrowserAgentJob,
    payload: dict[str, Any],
    finished_at: datetime,
) -> tuple[int, bool]:
    """Persist one successful local-browser result without committing.

    Browser-agent work is asynchronous and does not own a scheduler lease. The
    target and supplier product rows are locked directly, while the attempt keeps
    a stable synthetic lease token for auditability.
    """
    if job.monitor_target_id is None:
        raise BrowserAgentResultError("job is not linked to a monitor target")

    target = session.scalar(
        select(MonitorTarget)
        .where(MonitorTarget.id == job.monitor_target_id)
        .with_for_update()
    )
    if target is None:
        raise BrowserAgentResultError("monitor target no longer exists")

    row = session.execute(
        select(SupplierProduct, Supplier.id)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierProduct.id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(
            ProductBinding.id == target.product_binding_id,
            SupplierProduct.id == job.supplier_product_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise BrowserAgentResultError("job supplier product does not match monitor target")
    supplier_product, supplier_id = row

    offer = normalized_offer_from_agent(job, payload)
    started_at = job.created_at
    if started_at.tzinfo is None and finished_at.tzinfo is not None:
        started_at = started_at.replace(tzinfo=finished_at.tzinfo)

    attempt = MonitorAttempt(
        monitor_target_id=target.id,
        lease_token=f"browser-agent:{job.id}",
        outcome=AttemptOutcome.SUCCESS.value,
        adapter_code="ozon-browser-agent-v1",
        access_strategy=AccessStrategy.BROWSER.value,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
        http_status=200,
    )
    session.add(attempt)
    session.flush()

    fingerprint = offer.fingerprint
    state = session.scalar(
        select(SupplierOfferState)
        .where(SupplierOfferState.supplier_product_id == supplier_product.id)
        .with_for_update()
    )
    changed = state is None or state.fingerprint != fingerprint

    if state is None:
        state = SupplierOfferState(
            supplier_product_id=supplier_product.id,
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
        session.add(
            SupplierOfferObservation(
                supplier_product_id=supplier_product.id,
                monitor_attempt_id=attempt.id,
                price=offer.price,
                old_price=offer.old_price,
                available=offer.available,
                stock=offer.stock,
                delivery_days=offer.delivery_days,
                seller=offer.seller,
                fingerprint=fingerprint,
                adapter_schema_version=offer.adapter_schema_version,
                raw_metadata=json.dumps(
                    offer.raw_metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                observed_at=offer.observed_at,
            )
        )

    target.last_checked_at = finished_at
    target.consecutive_failures = 0
    apply_source_success(
        session,
        supplier_id=supplier_id,
        access_strategy=AccessStrategy.BROWSER.value,
        occurred_at=finished_at,
    )
    session.flush()
    return attempt.id, changed
