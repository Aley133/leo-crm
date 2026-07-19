from __future__ import annotations

import asyncio
import json
import os
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
        "available": offer.available,
        "stock": offer.stock,
        "delivery_days": offer.delivery_days,
        "seller": offer.seller,
        "adapter_schema_version": offer.adapter_schema_version,
        "observed_at": offer.observed_at.isoformat(),
        "raw_metadata": offer.raw_metadata,
    }


async def main() -> None:
    api_url = _required_env("CRM_API_URL").rstrip("/")
    token = _required_env("CRM_SERVICE_TOKEN")
    agent_id = (os.getenv("BROWSER_AGENT_ID") or "leo-local-chrome").strip()
    cdp_endpoint = (os.getenv("CHROME_CDP_ENDPOINT") or "http://127.0.0.1:9222").strip()
    poll_seconds = max(1.0, float(os.getenv("BROWSER_AGENT_POLL_SECONDS") or "3"))

    pool = PlaywrightBrowserPool(
        concurrency=1,
        cdp_endpoint=cdp_endpoint,
        reuse_default_context=True,
    )
    adapter = OzonBrowserAccessAdapter(pool)
    print(f"Browser agent {agent_id} connected to CRM {api_url}")
    print(f"Chrome CDP endpoint: {cdp_endpoint}")

    try:
        while True:
            claim = await asyncio.to_thread(
                _post_json,
                f"{api_url}/api/browser-agent/claim",
                token,
                {"agent_id": agent_id, "lease_seconds": 120},
            )
            job = claim.get("job")
            if not job:
                await asyncio.sleep(poll_seconds)
                continue

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

            await asyncio.to_thread(
                _post_json,
                f"{api_url}/api/browser-agent/jobs/{job['id']}/complete",
                token,
                completion,
            )
            print(f"Completed browser job #{job['id']}: {completion['status']}")
    finally:
        await adapter.close()


if __name__ == "__main__":
    started = time.time()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"Browser agent stopped after {int(time.time() - started)} seconds")
