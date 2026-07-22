from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .kaspi_http_transport import KaspiHttpSettings
from .models import MarketplaceOrder, MarketplaceOrderLine
from .product_identity_service import ensure_marketplace_listing_for_order_line


JOBS: dict[str, dict[str, Any]] = {}


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


def _text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_entry(
    entry: dict[str, Any],
    *,
    product: dict[str, Any] | None = None,
    merchant_product: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exact product-name priority from diagnostic archive v1.1.0."""

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
        ) or "Название не получено",
        "sku": _text(
            merchant_attrs.get("code"),
            merchant_attrs.get("sku"),
            product_attrs.get("code"),
            entry_attrs.get("offerCode"),
            entry_attrs.get("code"),
            entry_attrs.get("sku"),
        ),
        "external_product_id": (
            str((product or {}).get("id") or "")
            or _relationship_id(entry, "product", "masterProduct")
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


async def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(UTC).isoformat()
    job["message"] = "Загружаем точные названия товаров из Kaspi"

    try:
        settings = KaspiHttpSettings.from_environment()
        since = datetime.now(UTC) - timedelta(days=int(job["days"]))
        with SessionLocal() as session:
            rows = session.execute(
                select(MarketplaceOrder, MarketplaceOrderLine)
                .join(
                    MarketplaceOrderLine,
                    MarketplaceOrderLine.marketplace_order_id == MarketplaceOrder.id,
                )
                .where(MarketplaceOrder.ordered_at >= since)
                .order_by(MarketplaceOrder.id, MarketplaceOrderLine.id)
            ).all()
        job["total"] = len(rows)

        headers = {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": settings.api_token,
            "User-Agent": "leo-crm-product-enrichment/1.1.0",
        }
        merchant_cache: dict[str, dict[str, Any] | None] = {}
        semaphore = asyncio.Semaphore(3)

        async with httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(connect=10, read=15, write=10, pool=15),
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
        ) as client:
            async def get(path: str) -> dict[str, Any] | None:
                async with semaphore:
                    response = await client.get(path, headers=headers)
                job["request_count"] += 1
                response.raise_for_status()
                body = response.json()
                items = _data(body)
                return items[0] if items else None

            async def enrich(order: MarketplaceOrder, line: MarketplaceOrderLine) -> None:
                try:
                    entry_id = str(line.external_line_id or "").strip()
                    if not entry_id:
                        return
                    product = await get(f"/orderentries/{entry_id}/product")
                    master_id = (
                        str((product or {}).get("id") or "")
                        or _relationship_id({"relationships": {}}, "product", "masterProduct")
                    )
                    merchant = None
                    if master_id:
                        if master_id not in merchant_cache:
                            merchant_cache[master_id] = await get(
                                f"/masterproducts/{master_id}/merchantProduct"
                            )
                        merchant = merchant_cache[master_id]
                    normalized = normalize_entry(
                        {"id": entry_id, "attributes": {}},
                        product=product,
                        merchant_product=merchant,
                    )
                    with SessionLocal() as session:
                        with session.begin():
                            stored = session.get(MarketplaceOrderLine, line.id)
                            if stored is None:
                                return
                            changed = False
                            title = str(normalized["name"])
                            if title != "Название не получено" and stored.title != title:
                                stored.title = title
                                changed = True
                            sku = normalized.get("sku")
                            if sku and stored.merchant_sku != sku:
                                stored.merchant_sku = str(sku)
                                changed = True
                            product_id = normalized.get("external_product_id")
                            if product_id and stored.external_product_id != product_id:
                                stored.external_product_id = str(product_id)
                                changed = True
                            ensure_marketplace_listing_for_order_line(
                                session,
                                marketplace_account_id=order.marketplace_account_id,
                                order_line=stored,
                            )
                            if changed:
                                job["updated"] += 1
                except (httpx.HTTPError, ValueError) as exc:
                    job["errors"].append(
                        {
                            "order": order.external_code or order.external_order_id,
                            "line": line.id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                finally:
                    job["processed"] += 1
                    total = int(job["total"] or 0)
                    percent = round(job["processed"] * 100 / total, 1) if total else 100
                    job["message"] = (
                        f"Товары: {job['processed']}/{total}; обновлено: {job['updated']}; "
                        f"прогресс: {percent}%"
                    )

            await asyncio.gather(*(enrich(order, line) for order, line in rows))

        job["status"] = "completed" if not job["errors"] else "completed_with_errors"
        job["message"] = (
            f"Названия товаров загружены: обновлено {job['updated']}, "
            f"ошибок {len(job['errors'])}"
        )
    except Exception as exc:
        job["status"] = "failed"
        job["message"] = f"Обогащение остановлено: {type(exc).__name__}: {exc}"
        job["errors"].append({"error": repr(exc)})
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
