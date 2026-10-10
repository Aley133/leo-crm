from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import monotonic
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .kaspi_raw_receiver_jobs import ORDER_PERSIST_BATCH_SIZE, _persist_orders_in_batches
from .models import MarketplaceImportCheckpoint, MarketplaceOrder
from .workspace_kaspi import WorkspaceKaspiConnection


TERMINAL_ORDER_STATUSES = ("delivered", "cancelled", "returned")
DETAIL_WORKERS = 6
RECONCILIATION_BATCH_SIZE = 30
RECONCILIATION_CHECKPOINT_STREAM = "active_order_reconciliation"


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
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> tuple[tuple[ActiveOrderReference, ...], str | None]:
    """Return the next bounded slice of non-terminal orders.

    The regular raw receiver intentionally uses short ``creationDate`` windows
    for fast new-order intake. That makes it unsuitable for lifecycle repair:
    an order that remains local ``assembly`` after the seven-day deep window
    would otherwise never be observed again. A durable per-account cursor makes
    successive maintenance cycles cover the complete active set without one
    large request burst starving minute intake or local agents.
    """

    if limit < 1 or limit > 500:
        raise ValueError("active order reconciliation limit must be between 1 and 500")

    with SessionLocal() as session:
        checkpoint = session.scalar(
            select(MarketplaceImportCheckpoint).where(
                MarketplaceImportCheckpoint.marketplace_account_id
                == marketplace_account_id,
                MarketplaceImportCheckpoint.stream_name
                == RECONCILIATION_CHECKPOINT_STREAM,
            )
        )
        try:
            cursor = int(checkpoint.cursor or 0) if checkpoint is not None else 0
        except (TypeError, ValueError):
            cursor = 0

        conditions = (
            MarketplaceOrder.marketplace_account_id == marketplace_account_id,
            MarketplaceOrder.status.not_in(TERMINAL_ORDER_STATUSES),
            MarketplaceOrder.external_code.is_not(None),
            MarketplaceOrder.external_code != "",
        )
        rows = list(
            session.execute(
                select(MarketplaceOrder.id, MarketplaceOrder.external_code)
                .where(*conditions, MarketplaceOrder.id > cursor)
                .order_by(MarketplaceOrder.id)
                .limit(limit)
            ).all()
        )
        if len(rows) < limit and cursor > 0:
            rows.extend(
                session.execute(
                    select(MarketplaceOrder.id, MarketplaceOrder.external_code)
                    .where(*conditions, MarketplaceOrder.id <= cursor)
                    .order_by(MarketplaceOrder.id)
                    .limit(limit - len(rows))
                ).all()
            )

    references = tuple(
        ActiveOrderReference(order_id=int(order_id), external_code=str(external_code).strip())
        for order_id, external_code in rows
        if str(external_code or "").strip()
    )
    next_cursor = str(references[-1].order_id) if references else None
    return references, next_cursor


def _save_reconciliation_cursor(
    *,
    marketplace_account_id: int,
    cursor: str | None,
) -> None:
    if cursor is None:
        return
    with SessionLocal() as session:
        with session.begin():
            checkpoint = session.scalar(
                select(MarketplaceImportCheckpoint)
                .where(
                    MarketplaceImportCheckpoint.marketplace_account_id
                    == marketplace_account_id,
                    MarketplaceImportCheckpoint.stream_name
                    == RECONCILIATION_CHECKPOINT_STREAM,
                )
                .with_for_update()
            )
            if checkpoint is None:
                checkpoint = MarketplaceImportCheckpoint(
                    marketplace_account_id=marketplace_account_id,
                    stream_name=RECONCILIATION_CHECKPOINT_STREAM,
                )
                session.add(checkpoint)
            checkpoint.cursor = cursor


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
    *,
    batch_size: int = RECONCILIATION_BATCH_SIZE,
    time_budget_seconds: float | None = None,
) -> ActiveOrderReconciliationResult:
    """Refresh one bounded slice of CRM orders by exact Kaspi order code.

    Reads and HTTP calls stay off the event loop. Persistence reuses the normal
    raw receiver boundary, so status events, manual-override clearing, FIFO and
    XML updates remain transactional and idempotent.
    """

    started = monotonic()
    references, _ = await asyncio.to_thread(
        _load_active_order_references,
        marketplace_account_id=connection.account_id,
        limit=batch_size,
    )
    checked = found = imported = updated = missing = 0
    errors: list[str] = []
    for start in range(0, len(references), ORDER_PERSIST_BATCH_SIZE):
        chunk = references[start : start + ORDER_PERSIST_BATCH_SIZE]
        payloads, chunk_missing, chunk_errors = await asyncio.to_thread(
            _fetch_active_order_payloads, connection, chunk,
        )
        # Every committed group advances the cursor, even if a later group is
        # slow. Drain persistence plus checkpoint on cancellation before the
        # polling loop releases its single-flight lock.
        persistence = asyncio.create_task(
            _persist_reconciliation_chunk(connection, chunk, payloads)
        )
        try:
            chunk_imported, chunk_updated = await asyncio.shield(persistence)
        except asyncio.CancelledError:
            await persistence
            raise
        checked += len(chunk)
        found += len(payloads)
        imported += chunk_imported
        updated += chunk_updated
        missing += chunk_missing
        errors.extend(chunk_errors)
        if time_budget_seconds is not None and monotonic() - started >= time_budget_seconds:
            break
    return ActiveOrderReconciliationResult(
        checked=checked,
        found=found,
        imported=imported,
        updated=updated,
        missing=missing,
        errors=tuple(errors),
    )


async def _persist_reconciliation_chunk(
    connection: WorkspaceKaspiConnection,
    references: tuple[ActiveOrderReference, ...],
    payloads: list[dict[str, Any]],
) -> tuple[int, int]:
    imported = updated = 0
    if payloads:
        imported, updated = await _persist_orders_in_batches(
            payloads,
            timezone_name=connection.timezone,
            marketplace_account_id=connection.account_id,
        )
    await asyncio.to_thread(
        _save_reconciliation_cursor,
        marketplace_account_id=connection.account_id,
        cursor=str(references[-1].order_id),
    )
    return imported, updated
