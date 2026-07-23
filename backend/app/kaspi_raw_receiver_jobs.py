from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .inventory_service import allocate_order_line_fifo
from .kaspi_http_transport import KaspiHttpSettings
from .kaspi_integration import ensure_kaspi_marketplace_account
from .kaspi_order_payload import canonicalize_kaspi_order_payload
from .marketplace_import import import_kaspi_order
from .models import MarketplaceOrder, MarketplaceRawPayload
from .product_identity_service import ensure_marketplace_listing_for_order_line


KASPI_STATES: tuple[str, ...] = (
    "NEW",
    "SIGN_REQUIRED",
    "PICKUP",
    "DELIVERY",
    "KASPI_DELIVERY",
    "ARCHIVE",
)

# Kaspi occasionally exposes a newly created order before it appears in one of
# the documented state-filtered collections. Every manual rebuild therefore
# performs one additional unfiltered request per time slice and merges it with
# the state matrix. The state-filtered requests remain the compatibility path.
_ALL_STATES = "__ALL__"
JOBS: dict[str, dict[str, Any]] = {}


def create_job(*, days: int, timezone_name: str = "Asia/Almaty") -> str:
    if days < 1 or days > 31:
        raise ValueError("days must be between 1 and 31")
    job_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    ranges_per_day = len(KASPI_STATES) + 1
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "days": days,
        "timezone": timezone_name,
        "from_ms": int(start.timestamp() * 1000),
        "to_ms": int(now.timestamp() * 1000),
        "started_at": None,
        "finished_at": None,
        "progress": {"completed": 0, "total": days * ranges_per_day, "percent": 0},
        "request_count": 0,
        "orders_count": 0,
        "imported_count": 0,
        "updated_count": 0,
        "latest_order_at": None,
        "state_counts": {},
        "errors": [],
        "message": "Задание поставлено в очередь",
    }
    return job_id


def public_job(job_id: str) -> dict[str, Any] | None:
    job = JOBS.get(job_id)
    return dict(job) if job is not None else None


def _attrs(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("attributes")
    return value if isinstance(value, dict) else {}


def _items(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _meta(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    value = body.get("meta")
    return value if isinstance(value, dict) else {}


def _included_index(body: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    included = body.get("included")
    if not isinstance(included, list):
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in included:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("type") or ""), str(item.get("id") or ""))
        if all(key):
            result[key] = item
    return result


def _flatten_entry(
    entry: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    result = dict(entry)
    attrs = dict(entry.get("attributes") or {})
    relationships = entry.get("relationships") if isinstance(entry.get("relationships"), dict) else {}
    title_candidates = [attrs.get("name"), attrs.get("title")]
    sku_candidates = [attrs.get("offerCode"), attrs.get("merchantSku"), attrs.get("sku")]
    external_product_id = attrs.get("productId") or attrs.get("externalProductId")

    for relation_name in ("merchantProduct", "product", "masterProduct"):
        relation = relationships.get(relation_name)
        ref = relation.get("data") if isinstance(relation, dict) else None
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "")
        ref_id = str(ref.get("id") or "")
        external_product_id = external_product_id or ref_id or None
        resource = included.get((ref_type, ref_id), {})
        resource_attrs = resource.get("attributes") if isinstance(resource, dict) else None
        if isinstance(resource_attrs, dict):
            title_candidates.extend([resource_attrs.get("name"), resource_attrs.get("title")])
            sku_candidates.extend(
                [resource_attrs.get("code"), resource_attrs.get("sku"), resource_attrs.get("offerCode")]
            )

    attrs["name"] = next(
        (str(value).strip() for value in title_candidates if value not in (None, "") and str(value).strip()),
        "Unknown product",
    )
    sku = next(
        (str(value).strip() for value in sku_candidates if value not in (None, "") and str(value).strip()),
        None,
    )
    if sku is not None:
        attrs["offerCode"] = sku
    if external_product_id is not None:
        attrs["productId"] = str(external_product_id)
    result["attributes"] = attrs
    return result


def _entries_from_relationship(
    order: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    relationships = order.get("relationships") if isinstance(order.get("relationships"), dict) else {}
    relation = relationships.get("entries")
    refs = relation.get("data") if isinstance(relation, dict) else None
    if not isinstance(refs, list):
        return []
    entries: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        resource = included.get((str(ref.get("type") or ""), str(ref.get("id") or "")))
        if resource is not None:
            entries.append(_flatten_entry(resource, included))
    return entries


async def _fetch_entries(
    client: httpx.AsyncClient,
    *,
    order_id: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/orders/{order_id}/entries",
        params={"page[size]": 200, "include": "product,merchantProduct,masterProduct"},
        headers=headers,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        return []
    included = _included_index(body)
    return [_flatten_entry(entry, included) for entry in _items(body)]


async def _hydrate_page(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    *,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    included = _included_index(body)
    hydrated: list[dict[str, Any]] = []
    for order in _items(body):
        resource = dict(order)
        attrs = dict(order.get("attributes") or {})
        entries = _entries_from_relationship(order, included)
        order_id = str(order.get("id") or "").strip()
        if not entries and order_id:
            try:
                entries = await _fetch_entries(client, order_id=order_id, headers=headers)
            except (httpx.HTTPError, ValueError):
                # Preserve existing CRM lines by omitting the field when Kaspi's
                # secondary entries endpoint is temporarily unavailable.
                entries = []
        if entries:
            attrs["entries"] = entries
        else:
            attrs.pop("entries", None)
        resource["attributes"] = attrs
        hydrated.append(resource)
    return hydrated


def _delivery_cost(payload: dict[str, Any]) -> float:
    try:
        return float(_attrs(payload).get("deliveryCostForSeller") or 0)
    except (TypeError, ValueError):
        return 0.0


def _history_record(
    session,
    *,
    marketplace_account_id: int,
    external_order_id: str,
) -> dict[str, Any] | None:
    rows = session.execute(
        select(MarketplaceRawPayload.payload_json, MarketplaceRawPayload.received_at)
        .where(
            MarketplaceRawPayload.marketplace_account_id == marketplace_account_id,
            MarketplaceRawPayload.payload_type == "order",
            MarketplaceRawPayload.external_object_id == external_order_id,
        )
        .order_by(MarketplaceRawPayload.received_at.asc(), MarketplaceRawPayload.id.asc())
    ).all()
    previous_cost = 0.0
    for payload, received_at in rows:
        current_cost = _delivery_cost(payload if isinstance(payload, dict) else {})
        if current_cost > 0 and previous_cost <= 0:
            return {
                "transfer_started_at": received_at.isoformat(),
                "transfer_started_source": "delivery_cost_transition",
            }
        previous_cost = current_cost
    return None


def _persist_orders(orders: list[dict[str, Any]], *, timezone_name: str) -> tuple[int, int]:
    imported = 0
    updated = 0
    observed_at = datetime.now(UTC)
    with SessionLocal() as session:
        with session.begin():
            account = ensure_kaspi_marketplace_account(session)
            for source_payload in orders:
                external_order_id = str(
                    source_payload.get("id") or _attrs(source_payload).get("code") or ""
                ).strip()
                if not external_order_id:
                    continue
                history = _history_record(
                    session,
                    marketplace_account_id=account.id,
                    external_order_id=external_order_id,
                )
                payload = canonicalize_kaspi_order_payload(
                    source_payload,
                    now=observed_at,
                    history_record=history,
                    timezone_name=timezone_name,
                    handoff_cutoff_hour=21,
                )
                result = import_kaspi_order(
                    session,
                    marketplace_account_id=account.id,
                    payload=payload,
                    checkpoint_stream="raw_receiver_orders",
                )
                if result.created:
                    imported += 1
                elif result.changed:
                    updated += 1
                order = session.get(MarketplaceOrder, result.order_id)
                if order is not None:
                    for line in order.lines:
                        ensure_marketplace_listing_for_order_line(
                            session,
                            marketplace_account_id=account.id,
                            order_line=line,
                        )
                        # Product identity can be backfilled even for an unchanged
                        # order. Allocate after identity resolution on every manual
                        # rebuild; the FIFO service is idempotent per order line.
                        allocate_order_line_fifo(
                            session,
                            order_line=line,
                            order=order,
                            allocated_at=observed_at,
                        )
    return imported, updated


def _creation_ms(item: dict[str, Any]) -> int:
    value = _attrs(item).get("creationDate")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _state_name(item: dict[str, Any]) -> str:
    return str(_attrs(item).get("state") or "UNKNOWN").strip().upper() or "UNKNOWN"


async def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(UTC).isoformat()
    job["message"] = "Загрузка заказов из Kaspi"

    try:
        settings = KaspiHttpSettings.from_environment()
        day_ms = 24 * 60 * 60 * 1000
        min_split_ms = 60 * 60 * 1000
        base_chunks: list[tuple[str, int, int]] = []
        cursor = int(job["from_ms"])
        while cursor <= int(job["to_ms"]):
            end = min(cursor + day_ms - 1, int(job["to_ms"]))
            base_chunks.append((_ALL_STATES, cursor, end))
            for state in KASPI_STATES:
                base_chunks.append((state, cursor, end))
            cursor = end + 1

        unique: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        completed = 0
        lock = asyncio.Lock()
        queue: asyncio.Queue[tuple[str, int, int]] = asyncio.Queue()
        for chunk in base_chunks:
            queue.put_nowait(chunk)

        headers = {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": settings.api_token,
            "User-Agent": "leo-crm-raw-receiver/1.0.2",
        }

        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(connect=10, read=15, write=10, pool=15),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=5),
        ) as client:
            async def fetch_range(state: str, start_ms: int, end_ms: int) -> None:
                page_number = 0
                page_size = 50
                while True:
                    params: dict[str, Any] = {
                        "page[number]": page_number,
                        "page[size]": page_size,
                        "sort": "-creationDate",
                        "include": "entries",
                        "filter[orders][creationDate][$ge]": start_ms,
                        "filter[orders][creationDate][$le]": end_ms,
                    }
                    if state != _ALL_STATES:
                        params["filter[orders][state]"] = state
                    try:
                        response = await asyncio.wait_for(
                            client.get("/orders", params=params, headers=headers), timeout=15
                        )
                        async with lock:
                            job["request_count"] += 1
                        response.raise_for_status()
                        body = response.json()
                        page_items = await _hydrate_page(
                            client,
                            body if isinstance(body, dict) else {},
                            headers=headers,
                        )
                        async with lock:
                            for item in page_items:
                                key = str(item.get("id") or _attrs(item).get("code") or "")
                                if key:
                                    unique[key] = item
                            job["orders_count"] = len(unique)
                            label = "ALL" if state == _ALL_STATES else state
                            job["message"] = (
                                f"Получено {len(unique)} заказов; состояние {label}; "
                                f"страница {page_number + 1}"
                            )
                        meta = _meta(body)
                        page_count = meta.get("pageCount")
                        if not page_items:
                            return
                        if isinstance(page_count, int) and page_number + 1 >= page_count:
                            return
                        if not isinstance(page_count, int) and len(page_items) < page_size:
                            return
                        page_number += 1
                    except (TimeoutError, httpx.TimeoutException, httpx.RequestError) as exc:
                        async with lock:
                            job["request_count"] += 1
                        span = end_ms - start_ms + 1
                        if page_number == 0 and span > min_split_ms:
                            middle = start_ms + span // 2
                            await fetch_range(state, start_ms, middle - 1)
                            await fetch_range(state, middle, end_ms)
                            return
                        # The unfiltered request is an additional completeness probe.
                        # A deployment whose Kaspi endpoint requires state filters
                        # still succeeds through the documented state matrix.
                        if state == _ALL_STATES:
                            return
                        async with lock:
                            errors.append(
                                {
                                    "kind": "timeout",
                                    "state": state,
                                    "from_ms": start_ms,
                                    "to_ms": end_ms,
                                    "page": page_number,
                                    "error": type(exc).__name__,
                                }
                            )
                        return
                    except (httpx.HTTPStatusError, ValueError) as exc:
                        if state == _ALL_STATES:
                            return
                        async with lock:
                            errors.append(
                                {
                                    "kind": "http_or_json_error",
                                    "state": state,
                                    "from_ms": start_ms,
                                    "to_ms": end_ms,
                                    "page": page_number,
                                    "error": str(exc)[:500],
                                }
                            )
                        return

            async def worker() -> None:
                nonlocal completed
                while True:
                    try:
                        state, start_ms, end_ms = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await fetch_range(state, start_ms, end_ms)
                    finally:
                        async with lock:
                            completed += 1
                            total = len(base_chunks)
                            job["progress"] = {
                                "completed": completed,
                                "total": total,
                                "percent": round(completed * 100 / total, 1) if total else 100,
                            }
                            job["errors"] = list(errors)
                        queue.task_done()

            await asyncio.gather(*(worker() for _ in range(3)))

        orders = sorted(unique.values(), key=_creation_ms, reverse=True)
        state_counts = Counter(_state_name(item) for item in orders)
        latest_ms = max((_creation_ms(item) for item in orders), default=0)
        imported, updated = await asyncio.to_thread(
            _persist_orders,
            orders,
            timezone_name=str(job["timezone"]),
        )
        job["orders_count"] = len(orders)
        job["imported_count"] = imported
        job["updated_count"] = updated
        job["latest_order_at"] = (
            datetime.fromtimestamp(latest_ms / 1000, tz=UTC).isoformat() if latest_ms else None
        )
        job["state_counts"] = dict(sorted(state_counts.items()))
        job["errors"] = errors
        job["status"] = "completed" if not errors else "completed_with_errors"
        freshness = f", самый свежий: {job['latest_order_at']}" if job["latest_order_at"] else ""
        job["message"] = (
            f"Готово: {len(orders)} уникальных заказов{freshness}, "
            f"ошибок отдельных диапазонов: {len(errors)}"
        )
    except Exception as exc:
        job["status"] = "failed"
        job["message"] = f"Загрузка остановлена: {type(exc).__name__}: {exc}"
        job["errors"].append({"kind": "internal", "error": repr(exc)})
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
