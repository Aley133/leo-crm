from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.supplier_adapters.base import AdapterRequest
from backend.app.supplier_adapters.ozon_browser_access import OzonBrowserAccessAdapter
from backend.app.supplier_adapters.playwright_pool import PlaywrightBrowserPool


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


async def _run_job(job: dict, adapter: OzonBrowserAccessAdapter) -> dict:
    supplier_product_id = int(job["supplier_product_id"])
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


async def _complete_job(
    *,
    api_url: str,
    token: str,
    job: dict,
    adapter: OzonBrowserAccessAdapter,
) -> str:
    print(f"Claimed browser job #{job['id']}: {job['url']}")
    try:
        result = await _run_job(job, adapter)
        completion = {
            "lease_token": job["lease_token"],
            "status": "succeeded",
            "payload": result,
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


async def _dispatch_once(*, api_url: str, token: str, dispatch_limit: int) -> int:
    result = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/dispatch-due",
        token,
        {"limit": dispatch_limit, "supplier_code": "ozon"},
    )
    queued = int(result.get("queued_count") or 0)
    print(f"Dispatcher queued {queued} due monitor targets")
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
    adapter: OzonBrowserAccessAdapter,
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
                adapter=adapter,
            )
        except Exception as exc:
            print(f"Worker {worker_number} completion error for job #{job['id']}: {exc}")


async def _run_once(
    *,
    api_url: str,
    token: str,
    agent_id: str,
    adapter: OzonBrowserAccessAdapter,
    dispatch_limit: int,
) -> int:
    await _dispatch_once(api_url=api_url, token=token, dispatch_limit=dispatch_limit)
    job = await _claim_one(
        api_url=api_url,
        token=token,
        agent_id=f"{agent_id}-once",
    )
    if not job:
        print("No queued browser jobs. Queue one MonitorTarget in Swagger and run again.")
        return 2
    status = await _complete_job(
        api_url=api_url,
        token=token,
        job=job,
        adapter=adapter,
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
    adapter = OzonBrowserAccessAdapter(pool)
    print(f"Browser agent {agent_id} connected to CRM {api_url}")
    print(f"Chrome CDP endpoint: {cdp_endpoint}")
    print(f"Parallel browser workers: {1 if once else concurrency}")

    if once:
        try:
            return await _run_once(
                api_url=api_url,
                token=token,
                agent_id=agent_id,
                adapter=adapter,
                dispatch_limit=1,
            )
        finally:
            await adapter.close()

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
                adapter=adapter,
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
        await adapter.close()
    return 0


if __name__ == "__main__":
    started = time.time()
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(main(once=args.once)))
    except KeyboardInterrupt:
        print(f"Browser agent stopped after {int(time.time() - started)} seconds")
