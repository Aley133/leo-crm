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
_UNKNOWN_TITLES = {"", "Unknown product", "Название не получено"}


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


def _relationship_id(resource: dict[str, Any], *names: str) -> str | None:
    relationships = resource.get("relationships")
    if not isinstance(relationships, dict):
        return None
    for name in names:
        relation = relationships.get(name)
        data = relation.get("data") if isinstance(relation, dict) else None
        if isinstance(data, dict) and data.get("id") is not None:
            return str(data["id"])
    return None


def _text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_entry(
    entry: dict[str, Any],
    *,
    product: dict[str, Any] | None = None,
    merchant_product: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Archive v1.1.0 priority for exact product name and merchant SKU."""

    entry_attrs = _attrs(entry)
    product_attrs = _attrs(product)
    merchant_attrs = _attrs(merchant_product)
    return {
        "entry_id": str(entry.get("id") or ""),
        "name": _text(
            merchant_attrs.get("name"),
            merchant_attrs.get("title"),
            product_attrs.get("name"),
            product_attrs.get("title"),
            entry_attrs.get("name"),
            entry_attrs.get("title"),
            entry_attrs.get("productName"),
        )
        or "Название не получено",
        "sku": _text(
            merchant_attrs.get("code"),
            merchant_attrs.get("sku"),
            product_attrs.get("code"),
            product_attrs.get("sku"),
            entry_attrs.get("offerCode"),
            entry_attrs.get("merchantSku"),
            entry_attrs.get("code"),
            entry_attrs.get("sku"),
        ),
        "external_product_id": _text(
            (product or {}).get("id"),
            _relationship_id(entry, "product", "masterProduct"),
            entry_attrs.get("productId"),
            entry_attrs.get("externalProductId"),
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

    # A one-position Kaspi order must never remain Unknown only because the list
    # and detail endpoints used different temporary entry identifiers.
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
            "User-Agent": "leo-crm-product-enrichment/1.1.1",
        }
        semaphore = asyncio.Semaphore(3)
        cache_lock = asyncio.Lock()
        merchant_cache: dict[str, dict[str, Any] | None] = {}

        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(connect=10, read=20, write=10, pool=20),
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
        ) as client:

            async def get_data(
                path: str,
                *,
                order: str,
                step: str,
                entry: str | None = None,
            ) -> list[dict[str, Any]]:
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        async with semaphore:
                            response = await client.get(path, headers=headers)
                        job["request_count"] += 1
                        response.raise_for_status()
                        return _data(response.json())
                    except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
                        last_error = exc
                        if attempt < 3:
                            await asyncio.sleep(0.4 * attempt)
                error: dict[str, Any] = {
                    "order": order,
                    "step": step,
                    "path": path,
                    "error": f"{type(last_error).__name__}: {last_error}",
                }
                if entry:
                    error["entry"] = entry
                if isinstance(last_error, httpx.HTTPStatusError):
                    error["http_status"] = last_error.response.status_code
                job["errors"].append(error)
                return []

            async def merchant_for(master_id: str, *, order: str, entry: str) -> dict[str, Any] | None:
                async with cache_lock:
                    if master_id in merchant_cache:
                        return merchant_cache[master_id]
                resources = await get_data(
                    f"/masterproducts/{master_id}/merchantProduct",
                    order=order,
                    step="merchant_product",
                    entry=entry,
                )
                merchant = resources[0] if resources else None
                async with cache_lock:
                    merchant_cache[master_id] = merchant
                return merchant

            async def entries_for(
                external_id: str | None,
                code: str | None,
                internal_id: int,
            ) -> tuple[str, list[dict[str, Any]]]:
                keys: list[str] = []
                for candidate in (external_id, code, internal_id):
                    value = str(candidate or "").strip()
                    if value and value not in keys:
                        keys.append(value)
                for key in keys:
                    entries = await get_data(
                        f"/orders/{key}/entries",
                        order=key,
                        step="entries",
                    )
                    if entries:
                        return key, entries
                return (keys[0] if keys else str(internal_id)), []

            async def enrich_one(order_row: Any) -> None:
                order_id, external_order_id, external_code, account_id = order_row
                order_key = str(external_order_id or external_code or order_id)
                try:
                    order_key, entries = await entries_for(external_order_id, external_code, order_id)
                    if not entries:
                        job["errors"].append(
                            {"order": order_key, "step": "entries", "error": "Kaspi returned no order entries"}
                        )
                        return

                    normalized_items: list[dict[str, Any]] = []
                    for entry in entries:
                        entry_id = str(entry.get("id") or "").strip()
                        product: dict[str, Any] | None = None
                        merchant: dict[str, Any] | None = None
                        if entry_id:
                            products = await get_data(
                                f"/orderentries/{entry_id}/product",
                                order=order_key,
                                step="entry_product",
                                entry=entry_id,
                            )
                            product = products[0] if products else None
                        master_id = _text(
                            (product or {}).get("id"),
                            _relationship_id(entry, "product", "masterProduct"),
                        )
                        if master_id:
                            merchant = await merchant_for(master_id, order=order_key, entry=entry_id)
                        normalized_items.append(
                            normalize_entry(entry, product=product, merchant_product=merchant)
                        )

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
                                if title not in _UNKNOWN_TITLES and stored.title != title:
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
                                    job["linked"] += 1

                                before_allocated = sum(
                                    int(allocation.quantity or 0)
                                    for allocation in getattr(stored, "inventory_allocations", ())
                                )
                                allocation_result = allocate_order_line_fifo(
                                    session,
                                    order_line=stored,
                                    order=stored_order,
                                    allocated_at=datetime.now(UTC),
                                )
                                after_allocated = before_allocated + int(
                                    getattr(allocation_result, "allocated_quantity", 0) or 0
                                )
                                if after_allocated > before_allocated:
                                    job["allocated"] += after_allocated - before_allocated
                                if changed:
                                    job["updated"] += 1
                except Exception as exc:
                    job["errors"].append(
                        {
                            "order": order_key,
                            "step": "unexpected",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                finally:
                    job["processed"] += 1
                    total = int(job["total"] or 0)
                    percent = round(job["processed"] * 100 / total, 1) if total else 100
                    job["message"] = (
                        f"Заказы с товарами: {job['processed']}/{total}; "
                        f"обновлено: {job['updated']}; привязано: {job['linked']}; "
                        f"списано со склада: {job['allocated']}; прогресс: {percent}%"
                    )

            await asyncio.gather(*(enrich_one(row) for row in orders))

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
