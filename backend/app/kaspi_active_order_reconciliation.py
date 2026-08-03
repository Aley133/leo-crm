from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .kaspi_raw_receiver_jobs import _persist_orders_in_batches
from .models import MarketplaceOrder
from .workspace_kaspi import WorkspaceKaspiConnection


TERMINAL_ORDER_STATUSES = ("delivered", "cancelled", "returned")
DETAIL_WORKERS = 6


@dataclass(frozen=True, slots=True)
class ActiveOrderReference:
    order_id: int
    external_code: str


@dataclass(frozen=True, slots=True)
class ActiveOrderReconciliationResult:
    checked: int
    found: int
    imported: int
    updated: int
    missing: int
    errors: tuple[str, ...]


def _load_active_order_references(
    *,
    marketplace_account_id: int,
) -> tuple[ActiveOrderReference, ...]:
    """Return every locally non-terminal order, regardless of creation date.

    The regular raw receiver intentionally uses short ``creationDate`` windows
    for fast new-order intake. That makes it unsuitable for lifecycle repair:
    an order that remains local ``assembly`` after the seven-day deep window
    would otherwise never be observed again.
    """

    with SessionLocal() as session:
        rows = session.execute(
            select(MarketplaceOrder.id, MarketplaceOrder.external_code)
            .where(
                MarketplaceOrder.marketplace_account_id == marketplace_account_id,
                MarketplaceOrder.status.not_in(TERMINAL_ORDER_STATUSES),
                MarketplaceOrder.external_code.is_not(None),
                MarketplaceOrder.external_code != "",
            )
            .order_by(
                MarketplaceOrder.ordered_at.asc().nullsfirst(),
                MarketplaceOrder.id.asc(),
            )
        ).all()
    return tuple(
        ActiveOrderReference(order_id=int(order_id), external_code=str(external_code).strip())
        for order_id, external_code in rows
        if str(external_code or "").strip()
    )


def _fetch_active_order_payloads(
    connection: WorkspaceKaspiConnection,
    references: tuple[ActiveOrderReference, ...],
) -> tuple[list[dict[str, Any]], int, tuple[str, ...]]:
    if not references:
        return [], 0, ()

    transport = connection.transport(lookback_days=1)
    payloads: list[dict[str, Any]] = []
    missing = 0
    errors: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(references))) as executor:
            futures = {
                executor.submit(transport.fetch_order_by_code, reference.external_code): reference
                for reference in references
            }
            for future in as_completed(futures):
                reference = futures[future]
                try:
                    payload = future.result()
                except Exception as exc:  # one transient Kaspi error must not block the repair batch
                    errors.append(
                        f"{reference.external_code}: {type(exc).__name__}: {str(exc)[:240]}"
                    )
                    continue
                if payload is None:
                    missing += 1
                    continue
                if isinstance(payload, dict):
                    payloads.append(payload)
    finally:
        transport.close()
    return payloads, missing, tuple(errors)


async def reconcile_active_orders(
    connection: WorkspaceKaspiConnection,
) -> ActiveOrderReconciliationResult:
    """Refresh all non-terminal CRM orders by exact Kaspi order code.

    Reads and HTTP calls stay off the event loop. Persistence reuses the normal
    raw receiver boundary, so status events, manual-override clearing, FIFO and
    XML updates remain transactional and idempotent.
    """

    references = await asyncio.to_thread(
        _load_active_order_references,
        marketplace_account_id=connection.account_id,
    )
    payloads, missing, errors = await asyncio.to_thread(
        _fetch_active_order_payloads,
        connection,
        references,
    )
    imported = 0
    updated = 0
    if payloads:
        imported, updated = await _persist_orders_in_batches(
            payloads,
            timezone_name=connection.timezone,
            marketplace_account_id=connection.account_id,
        )
    return ActiveOrderReconciliationResult(
        checked=len(references),
        found=len(payloads),
        imported=imported,
        updated=updated,
        missing=missing,
        errors=errors,
    )
