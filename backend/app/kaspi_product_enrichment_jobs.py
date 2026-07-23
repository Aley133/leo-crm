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


def _relationship_id(resource: dict[str, Any], *names: str) -> str | None:
    relationships = resource.get("relationships")
    if not isinstance(relationships, dict):
        return None
    for name in names:
        relation = relationships.get(name)
        if not isinstance(relation, dict):
            continue
        data = relation.get("data")
        if isinstance(data, dict) and data.get("id") is not None:
            return str(data["id"])
    return None


def _number(*values: Any) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_entry(
    entry: dict[str, Any],
    *,
    product: dict[str, Any] | None = None,
    merchant_product: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one Kaspi entry exactly as archive v1.1.0 does.

    Product identity priority is authoritative and intentionally stable:
    merchantProduct -> product/masterProduct -> order entry.
    """

    entry_attrs = _attrs(entry)
    product_attrs = _attrs(product)
    merchant_attrs = _attrs(merchant_product)

    quantity = _number(entry_attrs.get("quantity"), entry_attrs.get("qty"), 1)
    if quantity <= 0:
        quantity = 1
    unit_price = _number(
        entry_attrs.get("basePrice"),
        entry_attrs.get("unitPrice"),
        entry_attrs.get("price"),
    )
    total_price = _number(
        entry_attrs.get("totalPrice"),
        entry_attrs.get("total"),
        unit_price * quantity,
    )
    if unit_price <= 0 and total_price > 0 and quantity > 0:
        unit_price = total_price / quantity

    name = _text(
        merchant_attrs.get("name"),
        merchant_attrs.get("title"),
        product_attrs.get("name"),
        product_attrs.get("title"),
        entry_attrs.get("name"),
        entry_attrs.get("title"),
        entry_attrs.get("productName"),
    ) or "Название не получено"

    sku = _text(
        merchant_attrs.get("code"),
        merchant_attrs.get("sku"),
        product_attrs.get("code"),
        entry_attrs.get("offerCode"),
        entry_attrs.get("code"),
        entry_attrs.get("sku"),
    )
    external_product_id = _text(
        merchant_attrs.get("productId"),
        product_attrs.get("code"),
        entry_attrs.get("productId"),
        entry_attrs.get("externalProductId"),
        (product or {}).get("id"),
        _relationship_id(entry, "product", "masterProduct"),
    )

    return {
        "entry_id": str(entry.get("id") or "").strip(),
        "name": name,
        "sku": sku,
        "external_product_id": external_product_id,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
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
    job["message"] = "Загружаем состав заказов по модели архива v1.1.0"

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
            "User-Agent": "leo-crm-product-enrichment/archive-v1.1.0",
        }
        request_semaphore = asyncio.Semaphore(3)
        counter_lock = asyncio.Lock()
        merchant_cache_lock = asyncio.Lock()
        merchant_cache: dict[str, dict[str, Any] | None] = {}

        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(connect=8, read=15, write=8, pool=15),
            limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
        ) as client:

            async def request_resource(
                path: str,
                *,
                order_key: str,
                step: str,
                params: dict[str, Any] | None = None,
                entry_id: str | None = None,
            ) -> dict[str, Any] | None:
                try:
                    async with request_semaphore:
                        response = await asyncio.wait_for(
                            client.get(path, params=params, headers=headers),
                            timeout=18,
                        )
                    async with counter_lock:
                        job["request_count"] += 1
                    response.raise_for_status()
                    body = response.json()
                    resources = _data(body)
                    return resources[0] if resources else None
                except (TimeoutError, httpx.HTTPError, ValueError) as exc:
                    async with counter_lock:
                        job["request_count"] += 1
                        job["errors"].append(
                            {
                                "order": order_key,
                                "entry": entry_id,
                                "step": step,
                                "path": path,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    return None

            async def request_entries(order_id: str, order_key: str) -> list[dict[str, Any]]:
                try:
                    async with request_semaphore:
                        response = await asyncio.wait_for(
                            client.get(
                                f"/orders/{order_id}/entries",
                                params={"page[size]": 200},
                                headers=headers,
                            ),
                            timeout=18,
                        )
                    async with counter_lock:
                        job["request_count"] += 1
                    response.raise_for_status()
                    return _data(response.json())
                except (TimeoutError, httpx.HTTPError, ValueError) as exc:
                    async with counter_lock:
                        job["request_count"] += 1
                        job["errors"].append(
                            {
                                "order": order_key,
                                "step": "entries",
                                "path": f"/orders/{order_id}/entries",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    return []

            async def merchant_for(
                master_id: str,
                *,
                order_key: str,
                entry_id: str,
            ) -> dict[str, Any] | None:
                async with merchant_cache_lock:
                    if master_id in merchant_cache:
                        return merchant_cache[master_id]
                resource = await request_resource(
                    f"/masterproducts/{master_id}/merchantProduct",
                    order_key=order_key,
                    entry_id=entry_id,
                    step="merchant_product",
                )
                async with merchant_cache_lock:
                    merchant_cache[master_id] = resource
                return resource

            queue: asyncio.Queue[Any] = asyncio.Queue()
            for row in orders:
                queue.put_nowait(row)

            async def enrich_one(order_row: Any) -> None:
                order_id, external_order_id, external_code, account_id = order_row
                order_key = str(external_code or external_order_id or order_id)
                kaspi_order_id = str(external_order_id or "").strip()
                if not kaspi_order_id:
                    async with counter_lock:
                        job["errors"].append(
                            {"order": order_key, "step": "entries", "error": "missing_order_id"}
                        )
                    return

                entries = await request_entries(kaspi_order_id, order_key)
                if not entries:
                    return

                async def enrich_entry(entry: dict[str, Any]) -> dict[str, Any]:
                    entry_id = str(entry.get("id") or "").strip()
                    product = None
                    merchant = None
                    if entry_id:
                        product = await request_resource(
                            f"/orderentries/{entry_id}/product",
                            order_key=order_key,
                            entry_id=entry_id,
                            step="entry_product",
                        )
                    master_id = str((product or {}).get("id") or "").strip()
                    if not master_id:
                        master_id = str(
                            _relationship_id(entry, "product", "masterProduct") or ""
                        ).strip()
                    if master_id:
                        merchant = await merchant_for(
                            master_id,
                            order_key=order_key,
                            entry_id=entry_id,
                        )
                    return normalize_entry(
                        entry,
                        product=product,
                        merchant_product=merchant,
                    )

                normalized_items = await asyncio.gather(
                    *(enrich_entry(entry) for entry in entries)
                )
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

                            allocation = allocate_order_line_fifo(
                                session,
                                order_line=stored,
                                order=stored_order,
                                allocated_at=datetime.now(UTC),
                            )
                            local_allocated += allocation.newly_allocated_quantity
                            if changed:
                                local_updated += 1

                async with counter_lock:
                    job["updated"] += local_updated
                    job["linked"] += local_linked
                    job["allocated"] += local_allocated

            async def worker() -> None:
                while True:
                    try:
                        row = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await enrich_one(row)
                    except Exception as exc:
                        async with counter_lock:
                            job["errors"].append(
                                {
                                    "order": str(row[2] or row[1] or row[0]),
                                    "step": "unexpected",
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            )
                    finally:
                        async with counter_lock:
                            job["processed"] += 1
                            total = int(job["total"] or 0)
                            percent = round(job["processed"] * 100 / total, 1) if total else 100
                            job["message"] = (
                                f"Товары: {job['processed']}/{total}; "
                                f"обновлено: {job['updated']}; привязано: {job['linked']}; "
                                f"списано: {job['allocated']}; прогресс: {percent}%"
                            )
                        queue.task_done()

            await asyncio.gather(*(worker() for _ in range(3)))

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
