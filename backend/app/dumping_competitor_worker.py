from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .dumping_models import DumpingPolicy

# This queue is intentionally independent from Browser Agent.  It does not use
# browser_agent_jobs, leases, heartbeats or the local Windows agent.  Supplier
# observations may enqueue product IDs after their own transaction commits, but
# competitor HTTP work is executed only by this worker.
_QUEUE: asyncio.Queue[int] = asyncio.Queue()
_PENDING: set[int] = set()
_STATES: dict[int, dict[str, Any]] = {}
_DELAYED_TASKS: set[asyncio.Task] = set()
_STOP_EVENT: asyncio.Event | None = None
_WORKER_TASK: asyncio.Task | None = None
_SCHEDULER_TASK: asyncio.Task | None = None

MIN_REQUEST_INTERVAL_SECONDS = 8.0
PERIODIC_REFRESH_SECONDS = 10 * 60
MAX_BACKOFF_SECONDS = 30 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def state_for_product(product_id: int) -> dict[str, Any] | None:
    state = _STATES.get(product_id)
    return None if state is None else dict(state)


def _set_state(product_id: int, **values: Any) -> None:
    current = _STATES.setdefault(product_id, {})
    current.update(values)
    current["updated_at"] = _now().isoformat()


def enqueue_competitor_scan(product_id: int, *, reason: str = "manual") -> bool:
    """Put one product into the dedicated competitor queue once.

    Safe to call from synchronous supplier-ingestion code.  No Browser Agent
    job is created and no external request is performed by this function.
    """
    if product_id in _PENDING:
        return False
    _PENDING.add(product_id)
    _set_state(product_id, status="queued", reason=reason, last_error=None)
    try:
        _QUEUE.put_nowait(product_id)
    except RuntimeError:
        _PENDING.discard(product_id)
        _set_state(product_id, status="worker_unavailable", reason=reason)
        return False
    return True


async def _enqueue_later(product_id: int, delay_seconds: float, reason: str) -> None:
    await asyncio.sleep(max(1.0, delay_seconds))
    enqueue_competitor_scan(product_id, reason=reason)


def _schedule_retry(product_id: int, delay_seconds: float, reason: str) -> None:
    task = asyncio.create_task(_enqueue_later(product_id, delay_seconds, reason))
    _DELAYED_TASKS.add(task)
    task.add_done_callback(_DELAYED_TASKS.discard)


def _retry_after_seconds(exc: httpx.HTTPStatusError, attempts: int) -> float:
    header = exc.response.headers.get("Retry-After")
    if header:
        try:
            return min(MAX_BACKOFF_SECONDS, max(30.0, float(header)))
        except ValueError:
            pass
    return min(MAX_BACKOFF_SECONDS, 60.0 * (2 ** max(0, attempts - 1)))


async def _worker_loop(stop_event: asyncio.Event) -> None:
    # Imported lazily to avoid an import cycle: dumping_runner imports enqueue.
    from .dumping_runner import execute_dumping_for_product

    last_request_at = 0.0
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            product_id = await asyncio.wait_for(_QUEUE.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        _PENDING.discard(product_id)
        state = _STATES.get(product_id, {})
        attempts = int(state.get("attempts") or 0) + 1
        elapsed = loop.time() - last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            await asyncio.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        _set_state(product_id, status="scanning", attempts=attempts, started_at=_now().isoformat())
        try:
            with SessionLocal() as db:
                await execute_dumping_for_product(db, product_id)
            _set_state(
                product_id,
                status="completed",
                attempts=0,
                last_error=None,
                next_retry_at=None,
                finished_at=_now().isoformat(),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                delay = _retry_after_seconds(exc, attempts)
                next_retry = _now() + timedelta(seconds=delay)
                _set_state(
                    product_id,
                    status="retry_wait",
                    attempts=attempts,
                    last_error="Kaspi временно ограничил частоту запросов (429)",
                    next_retry_at=next_retry.isoformat(),
                )
                _schedule_retry(product_id, delay, "retry_after_429")
            else:
                _set_state(
                    product_id,
                    status="failed",
                    attempts=attempts,
                    last_error=f"Kaspi HTTP {exc.response.status_code}",
                    next_retry_at=None,
                )
        except (ValueError, RuntimeError) as exc:
            _set_state(product_id, status="blocked", attempts=attempts, last_error=str(exc), next_retry_at=None)
        except Exception as exc:  # keep the queue alive on isolated product failures
            delay = min(MAX_BACKOFF_SECONDS, 60.0 * (2 ** max(0, attempts - 1)))
            next_retry = _now() + timedelta(seconds=delay)
            _set_state(
                product_id,
                status="retry_wait",
                attempts=attempts,
                last_error=f"{type(exc).__name__}: {exc}",
                next_retry_at=next_retry.isoformat(),
            )
            _schedule_retry(product_id, delay, "retry_after_error")
        finally:
            last_request_at = loop.time()
            _QUEUE.task_done()


async def _scheduler_loop(stop_event: asyncio.Event) -> None:
    # Small startup delay prevents deployment startup from immediately hitting Kaspi.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=45.0)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        with SessionLocal() as db:
            product_ids = list(
                db.scalars(
                    select(DumpingPolicy.product_id).where(
                        DumpingPolicy.enabled.is_(True),
                        DumpingPolicy.auto_publish_xml.is_(True),
                    )
                )
            )
        random.shuffle(product_ids)
        for product_id in product_ids:
            enqueue_competitor_scan(product_id, reason="periodic_refresh")
        wait_for = PERIODIC_REFRESH_SECONDS + random.randint(0, 90)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_for)
        except asyncio.TimeoutError:
            continue


async def start_dumping_competitor_worker() -> None:
    global _STOP_EVENT, _WORKER_TASK, _SCHEDULER_TASK
    if _WORKER_TASK is not None and not _WORKER_TASK.done():
        return
    _STOP_EVENT = asyncio.Event()
    _WORKER_TASK = asyncio.create_task(_worker_loop(_STOP_EVENT), name="dumping-competitor-worker")
    _SCHEDULER_TASK = asyncio.create_task(_scheduler_loop(_STOP_EVENT), name="dumping-competitor-scheduler")


async def stop_dumping_competitor_worker() -> None:
    global _STOP_EVENT, _WORKER_TASK, _SCHEDULER_TASK
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()
    for task in (_WORKER_TASK, _SCHEDULER_TASK):
        if task is not None:
            task.cancel()
    for task in tuple(_DELAYED_TASKS):
        task.cancel()
    for task in (_WORKER_TASK, _SCHEDULER_TASK):
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
    _WORKER_TASK = None
    _SCHEDULER_TASK = None
    _STOP_EVENT = None
