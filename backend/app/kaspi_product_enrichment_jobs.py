from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .inventory_service import allocate_order_line_fifo
from .kaspi_http_transport import KaspiHttpSettings
from .models import MarketplaceOrder, MarketplaceOrderLine
from .product_identity_service import ensure_marketplace_listing_for_order_line


JOBS: dict[str, dict[str, Any]] = {}
_UNKNOWN_TITLES = {"", "unknown product", "название не получено"}


def _data(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    value = body.get("data")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _attrs(resource: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    value = resource.get("attributes")
    return value if isinstance(value, dict) else {}


def _text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


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


def _entries_from_order(body: dict[str, Any], order: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = _attrs(order)
    embedded = attributes.get("entries")
    if isinstance(embedded, list):
        result = [item for item in embedded if isinstance(item, dict)]
        if result:
            return result

    relationships = order.get("relationships")
    relationships = relationships if isinstance(relationships, dict) else {}
    relation = relationships.get("entries")
    references = relation.get("data") if isinstance(relation, dict) else None
    if not isinstance(references, list):
        return []

    included = _included_index(body)
    entries: list[dict[str, Any]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        key = (str(reference.get("type") or ""), str(reference.get("id") or ""))
        resource = included.get(key)
        if resource is not None:
            entries.append(resource)
    return entries


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Read the exact product identity already returned by Kaspi order entries."""

    attributes = _attrs(entry)
    offer = attributes.get("offer")
    offer = offer if isinstance(offer, dict) else {}
    return {
        "entry_id": str(entry.get("id") or "").strip(),
        "name": _text(
            attributes.get("name"),
            offer.get("name"),
            attributes.get("title"),
            attributes.get("productName"),
        )
        or "Название не получено",
        "sku": _text(
            attributes.get("offerCode"),
            offer.get("code"),
            attributes.get("merchantSku"),
            attributes.get("sku"),
            attributes.get("code"),
        ),
        "external_product_id": _text(
            attributes.get("productId"),
            attributes.get("externalProductId"),
        ),
    }


def create_job(*, days: int = 31) -> str:
    if days < 1 or days > 31:
        raise ValueError("days must be between 1 and 31")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "days": days,
        "processed": 0,
        "total": 0,
        "updated": 0,
        "linked": 0,
        "allocated": 0,
        "request_count": 0,
        "errors": [],
        "message": "Обогащение товаров поставлено в очередь",
        "started_at": None,
        "finished_at": None,
    }
    return job_id


def public_job(job_id: str) -> dict[str, Any] | None:
    job = JOBS.get(job_id)
    return dict(job) if job is not None else None


def _match_line(
    lines: list[MarketplaceOrderLine],
    normalized: dict[str, Any],
    *,
    normalized_count: int,
) -> MarketplaceOrderLine | None:
    entry_id = str(normalized.get("entry_id") or "").strip()
    sku = str(normalized.get("sku") or "").strip()
    product_id = str(normalized.get("external_product_id") or "").strip()

    for line in lines:
        if entry_id and str(line.external_line_id or "").strip() == entry_id:
            return line
    for line in lines:
        if sku and str(line.merchant_sku or "").strip() == sku:
            return line
    for line in lines:
        if product_id and str(line.external_product_id or "").strip() == product_id:
            return line
    if normalized_count == 1 and len(lines) == 1:
        return lines[0]
    return None


async def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(UTC).isoformat()
    job["message"] = "Загружаем точные названия и артикулы из Kaspi"

    try:
        settings = KaspiHttpSettings.from_environment()
        since = datetime.now(UTC) - timedelta(days=int(job["days"]))
        with SessionLocal() as session:
            orders = session.execute(
                select(
                    MarketplaceOrder.id,
                    MarketplaceOrder.external_order_id,
                    MarketplaceOrder.external_code,
                    MarketplaceOrder.marketplace_account_id,
                )
                .where(MarketplaceOrder.ordered_at >= since)
                .order_by(MarketplaceOrder.id)
            ).all()
        job["total"] = len(orders)

        headers = {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": settings.api_token,
            "User-Agent": "leo-crm-product-enrichment/1.2.0",
        }
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for row in orders:
            queue.put_nowait(row)
        counters_lock = asyncio.Lock()

        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(connect=8, read=12, write=8, pool=12),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:

            async def request_json(
                path: str,
                *,
                params: dict[str, Any] | None,
                order_code: str,
                step: str,
            ) -> dict[str, Any] | None:
                try:
                    response = await asyncio.wait_for(
                        client.get(path, params=params, headers=headers),
                        timeout=15,
                    )
                    async with counters_lock:
                        job["request_count"] += 1
                    response.raise_for_status()
                    body = response.json()
                    return body if isinstance(body, dict) else None
                except (TimeoutError, httpx.HTTPError, ValueError) as exc:
                    async with counters_lock:
                        job["errors"].append(
                            {
                                "order": order_code,
                                "step": step,
                                "path": path,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    return None

            async def load_entries(
                external_order_id: str | None,
                external_code: str | None,
            ) -> tuple[str, list[dict[str, Any]]]:
                code = str(external_code or "").strip()
                external_id = str(external_order_id or "").strip()
                lookup = code or external_id

                if code:
                    body = await request_json(
                        "/orders",
                        params={
                            "filter[orders][code]": code,
                            "include": "entries",
                            "page[number]": 0,
                            "page[size]": 1,
                        },
                        order_code=code,
                        step="order_by_code",
                    )
                    orders_data = _data(body)
                    if body and orders_data:
                        entries = _entries_from_order(body, orders_data[0])
                        if entries:
                            return code, entries
                        external_id = str(orders_data[0].get("id") or external_id).strip()

                if external_id:
                    body = await request_json(
                        f"/orders/{external_id}/entries",
                        params={"page[size]": 200},
                        order_code=lookup,
                        step="entries_fallback",
                    )
                    entries = _data(body)
                    if entries:
                        return lookup, entries
                return lookup, []

            async def enrich_one(order_row: Any) -> None:
                order_id, external_order_id, external_code, account_id = order_row
                order_key = str(external_code or external_order_id or order_id)
                try:
                    order_key, entries = await load_entries(external_order_id, external_code)
                    if not entries:
                        async with counters_lock:
                            job["errors"].append(
                                {
                                    "order": order_key,
                                    "step": "entries",
                                    "error": "Kaspi returned no order entries",
                                }
                            )
                        return

                    normalized_items = [normalize_entry(entry) for entry in entries]
                    local_updated = 0
                    local_linked = 0
                    local_allocated = 0
                    with SessionLocal() as session:
                        with session.begin():
                            stored_order = session.get(MarketplaceOrder, order_id)
                            if stored_order is None:
                                return
                            lines = list(
                                session.scalars(
                                    select(MarketplaceOrderLine)
                                    .where(MarketplaceOrderLine.marketplace_order_id == order_id)
                                    .order_by(MarketplaceOrderLine.id)
                                ).all()
                            )
                            for normalized in normalized_items:
                                stored = _match_line(
                                    lines,
                                    normalized,
                                    normalized_count=len(normalized_items),
                                )
                                if stored is None:
                                    job["errors"].append(
                                        {
                                            "order": order_key,
                                            "entry": normalized.get("entry_id"),
                                            "step": "persist",
                                            "error": "matching_order_line_not_found",
                                        }
                                    )
                                    continue

                                changed = False
                                title = str(normalized.get("name") or "").strip()
                                if title.casefold() not in _UNKNOWN_TITLES and stored.title != title:
                                    stored.title = title
                                    changed = True
                                sku = _text(normalized.get("sku"))
                                if sku and stored.merchant_sku != sku:
                                    stored.merchant_sku = sku
                                    changed = True
                                external_product_id = _text(normalized.get("external_product_id"))
                                if external_product_id and stored.external_product_id != external_product_id:
                                    stored.external_product_id = external_product_id
                                    changed = True
                                entry_id = _text(normalized.get("entry_id"))
                                if entry_id and stored.external_line_id != entry_id:
                                    stored.external_line_id = entry_id
                                    changed = True

                                before_product_id = stored.product_id
                                ensure_marketplace_listing_for_order_line(
                                    session,
                                    marketplace_account_id=account_id,
                                    order_line=stored,
                                )
                                if before_product_id is None and stored.product_id is not None:
                                    local_linked += 1

                                allocation_result = allocate_order_line_fifo(
                                    session,
                                    order_line=stored,
                                    order=stored_order,
                                    allocated_at=datetime.now(UTC),
                                )
                                local_allocated += allocation_result.newly_allocated_quantity
                                if changed:
                                    local_updated += 1

                    async with counters_lock:
                        job["updated"] += local_updated
                        job["linked"] += local_linked
                        job["allocated"] += local_allocated
                except Exception as exc:
                    async with counters_lock:
                        job["errors"].append(
                            {
                                "order": order_key,
                                "step": "unexpected",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                finally:
                    async with counters_lock:
                        job["processed"] += 1
                        total = int(job["total"] or 0)
                        percent = round(job["processed"] * 100 / total, 1) if total else 100
                        job["message"] = (
                            f"Заказы с товарами: {job['processed']}/{total}; "
                            f"обновлено: {job['updated']}; привязано: {job['linked']}; "
                            f"списано со склада: {job['allocated']}; прогресс: {percent}%"
                        )

            async def worker() -> None:
                while True:
                    try:
                        row = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await enrich_one(row)
                    finally:
                        queue.task_done()

            await asyncio.gather(*(worker() for _ in range(4)))

        job["status"] = "completed" if not job["errors"] else "completed_with_errors"
        job["message"] = (
            f"Товары загружены: обновлено строк {job['updated']}, "
            f"привязано {job['linked']}, списано со склада {job['allocated']}, "
            f"ошибок {len(job['errors'])}"
        )
    except Exception as exc:
        job["status"] = "failed"
        job["message"] = f"Обогащение остановлено: {type(exc).__name__}: {exc}"
        job["errors"].append({"step": "internal", "error": repr(exc)})
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
