from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from backend.app.supplier_adapters.base import AdapterRequest
from backend.app.supplier_adapters.ozon_browser_access import OzonBrowserAccessAdapter
from backend.app.supplier_adapters.playwright_pool import PlaywrightBrowserPool
from backend.app.supplier_adapters.wildberries_browser_access import WildberriesBrowserAccessAdapter
from tools.kaspi_seller_browser import (
    KaspiSellerBrowserAdapter,
    KaspiSellerOrderRequest,
)


SUPPLIER_JOB_TYPE = "supplier_product_observation"
KASPI_SELLER_JOB_TYPE = "kaspi_seller_order_details"
DEFAULT_JOB_TIMEOUT_SECONDS = 120.0


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _post_json(url: str, token: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CRM returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"CRM is unavailable: {exc}") from exc


def _adapter_code_for_url(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    if host in {"ozon.ru", "ozon.kz"} or host.endswith(".ozon.ru") or host.endswith(".ozon.kz"):
        return "ozon"
    if host in {"wildberries.ru", "wb.ru"} or host.endswith(".wildberries.ru"):
        return "wb"
    raise ValueError(f"Unsupported supplier URL host: {host or '-'}")


def _job_type(job: dict) -> str:
    return str(job.get("job_type") or SUPPLIER_JOB_TYPE).strip()


async def _run_supplier_job(job: dict, adapters: dict[str, Any]) -> dict:
    supplier_product_id = int(job["supplier_product_id"])
    adapter_code = _adapter_code_for_url(str(job["url"]))
    adapter = adapters[adapter_code]
    offer = await adapter.fetch(
        AdapterRequest(
            supplier_product_id=supplier_product_id,
            url=str(job["url"]),
            external_id=f"browser-agent-{supplier_product_id}",
        )
    )
    return {
        "price": str(offer.price) if offer.price is not None else None,
        "old_price": str(offer.old_price) if offer.old_price is not None else None,
        "currency": offer.currency,
        "available": offer.available,
        "stock": offer.stock,
        "delivery_days": offer.delivery_days,
        "seller": offer.seller,
        "adapter_schema_version": offer.adapter_schema_version,
        "observed_at": offer.observed_at.isoformat(),
        "raw_metadata": offer.raw_metadata,
    }


async def _run_kaspi_seller_job(job: dict, adapters: dict[str, Any]) -> dict:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Kaspi Seller browser job payload is required")
    merchant_id = str(payload.get("merchant_id") or "").strip()
    order_code = str(payload.get("order_code") or "").strip()
    if not merchant_id or not order_code:
        raise ValueError("Kaspi Seller job requires merchant_id and order_code")

    adapter = adapters.get("kaspi_seller")
    if adapter is None:
        raise RuntimeError("Kaspi Seller browser adapter is not configured")
    return await adapter.fetch_order(
        KaspiSellerOrderRequest(
            merchant_id=merchant_id,
            order_code=order_code,
        )
    )


async def _run_job(job: dict, adapters: dict[str, Any]) -> dict:
    job_type = _job_type(job)
    if job_type == KASPI_SELLER_JOB_TYPE:
        return await _run_kaspi_seller_job(job, adapters)
    if job_type == SUPPLIER_JOB_TYPE:
        return await _run_supplier_job(job, adapters)
    raise ValueError(f"Unsupported browser agent job_type: {job_type}")


def _job_description(job: dict) -> str:
    if _job_type(job) == KASPI_SELLER_JOB_TYPE:
        payload = job.get("payload") or {}
        return f"Kaspi Seller order {payload.get('order_code') or '-'}"
    return str(job.get("url") or "-")


async def _complete_job(
    *,
    api_url: str,
    token: str,
    job: dict,
    adapters: dict[str, Any],
) -> str:
    print(f"Claimed browser job #{job['id']}: {_job_description(job)}")
    timeout_seconds = max(
        10.0,
        float(os.getenv("BROWSER_AGENT_JOB_TIMEOUT_SECONDS") or DEFAULT_JOB_TIMEOUT_SECONDS),
    )
    try:
        result = await asyncio.wait_for(
            _run_job(job, adapters),
            timeout=timeout_seconds,
        )
        completion = {
            "lease_token": job["lease_token"],
            "status": "succeeded",
            "payload": result,
        }
    except TimeoutError:
        completion = {
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": "BrowserAgentJobTimeout",
            "error_message": f"Browser job exceeded {timeout_seconds:g} seconds",
        }
    except Exception as exc:
        completion = {
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": exc.__class__.__name__,
            "error_message": str(exc)[:4000],
        }

    response = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/jobs/{job['id']}/complete",
        token,
        completion,
    )
    print(f"Completed browser job #{job['id']}: {completion['status']}")
    if completion["status"] == "succeeded":
        print(json.dumps(completion["payload"], ensure_ascii=False, indent=2))
    else:
        print(completion["error_message"])
    return str(response.get("status") or completion["status"])


async def _claim_one(
    *,
    api_url: str,
    token: str,
    agent_id: str,
    lease_seconds: int = 180,
) -> dict | None:
    claim = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/claim",
        token,
        {
            "agent_id": agent_id,
            "lease_seconds": lease_seconds,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "version": (os.getenv("BROWSER_AGENT_VERSION") or "dev").strip(),
        },
    )
    return claim.get("job")


async def _dispatch_source_once(*, api_url: str, token: str, dispatch_limit: int, supplier_code: str) -> int:
    result = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/dispatch-due",
        token,
        {"limit": dispatch_limit, "supplier_code": supplier_code},
    )
    queued = int(result.get("queued_count") or 0)
    print(f"Dispatcher queued {queued} due {supplier_code} monitor targets")
    return queued


async def _dispatch_once(*, api_url: str, token: str, dispatch_limit: int) -> int:
    queued = 0
    for supplier_code in ("ozon", "wb"):
        queued += await _dispatch_source_once(
            api_url=api_url,
            token=token,
            dispatch_limit=dispatch_limit,
            supplier_code=supplier_code,
        )
    return queued


async def _dispatch_loop(
    *,
    api_url: str,
    token: str,
    poll_seconds: float,
    dispatch_limit: int,
) -> None:
    while True:
        try:
            await _dispatch_once(
                api_url=api_url,
                token=token,
                dispatch_limit=dispatch_limit,
            )
        except Exception as exc:
            print(f"Dispatcher error: {exc}")
        await asyncio.sleep(poll_seconds)


async def _worker_loop(
    *,
    worker_number: int,
    api_url: str,
    token: str,
    agent_id: str,
    adapters: dict[str, Any],
    poll_seconds: float,
) -> None:
    worker_id = f"{agent_id}-w{worker_number}"
    while True:
        try:
            job = await _claim_one(
                api_url=api_url,
                token=token,
                agent_id=worker_id,
            )
        except Exception as exc:
            print(f"Worker {worker_number} claim error: {exc}")
            await asyncio.sleep(poll_seconds)
            continue

        if not job:
            await asyncio.sleep(poll_seconds)
            continue

        try:
            await _complete_job(
                api_url=api_url,
                token=token,
                job=job,
                adapters=adapters,
            )
        except Exception as exc:
            print(f"Worker {worker_number} completion error for job #{job['id']}: {exc}")


async def _run_once(
    *,
    api_url: str,
    token: str,
    agent_id: str,
    adapters: dict[str, Any],
    dispatch_limit: int,
) -> int:
    await _dispatch_once(api_url=api_url, token=token, dispatch_limit=dispatch_limit)
    job = await _claim_one(
        api_url=api_url,
        token=token,
        agent_id=f"{agent_id}-once",
    )
    if not job:
        print("No queued browser jobs. Queue one MonitorTarget or Kaspi Seller job in CRM and run again.")
        return 2
    status = await _complete_job(
        api_url=api_url,
        token=token,
        job=job,
        adapters=adapters,
    )
    return 0 if status == "succeeded" else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LEO CRM local Chrome browser agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="dispatch and process exactly one job, then exit",
    )
    return parser.parse_args()


async def main(*, once: bool = False) -> int:
    api_url = _required_env("CRM_API_URL").rstrip("/")
    token = _required_env("CRM_SERVICE_TOKEN")
    agent_id = (os.getenv("BROWSER_AGENT_ID") or "leo-local-chrome").strip()
    cdp_endpoint = (os.getenv("CHROME_CDP_ENDPOINT") or "http://127.0.0.1:9222").strip()
    poll_seconds = max(1.0, float(os.getenv("BROWSER_AGENT_POLL_SECONDS") or "3"))
    concurrency = max(1, min(12, int(os.getenv("BROWSER_AGENT_CONCURRENCY") or "3")))
    dispatch_limit = max(1, min(1000, int(os.getenv("BROWSER_AGENT_DISPATCH_LIMIT") or "100")))

    pool = PlaywrightBrowserPool(
        concurrency=1 if once else concurrency,
        cdp_endpoint=cdp_endpoint,
        reuse_default_context=True,
    )
    adapters: dict[str, Any] = {
        "ozon": OzonBrowserAccessAdapter(pool),
        "wb": WildberriesBrowserAccessAdapter(pool),
        "kaspi_seller": KaspiSellerBrowserAdapter(pool),
    }
    print(f"Browser agent {agent_id} connected to CRM {api_url}")
    print(f"Chrome CDP endpoint: {cdp_endpoint}")
    print(f"Parallel browser workers: {1 if once else concurrency}")
    print(f"Browser job timeout: {max(10.0, float(os.getenv('BROWSER_AGENT_JOB_TIMEOUT_SECONDS') or DEFAULT_JOB_TIMEOUT_SECONDS)):g}s")
    print("Enabled adapters: ozon, wb, kaspi_seller")

    if once:
        try:
            return await _run_once(
                api_url=api_url,
                token=token,
                agent_id=agent_id,
                adapters=adapters,
                dispatch_limit=1,
            )
        finally:
            await pool.close()

    tasks = [
        asyncio.create_task(
            _dispatch_loop(
                api_url=api_url,
                token=token,
                poll_seconds=poll_seconds,
                dispatch_limit=dispatch_limit,
            ),
            name="browser-agent-dispatcher",
        )
    ]
    tasks.extend(
        asyncio.create_task(
            _worker_loop(
                worker_number=number,
                api_url=api_url,
                token=token,
                agent_id=agent_id,
                adapters=adapters,
                poll_seconds=poll_seconds,
            ),
            name=f"browser-agent-worker-{number}",
        )
        for number in range(1, concurrency + 1)
    )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.close()
    return 0


if __name__ == "__main__":
    started = time.time()
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(main(once=args.once)))
    except KeyboardInterrupt:
        print(f"Browser agent stopped after {int(time.time() - started)} seconds")
