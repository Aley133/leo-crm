from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import platform
import socket
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.kaspi_offer_competitor import scan_kaspi_competitors

VERSION = "1.0.0"
DEFAULT_API_URL = "https://leo-crm-api.onrender.com"


def _service_token() -> str:
    value = (os.getenv("CRM_SERVICE_TOKEN") or "").strip()
    if value:
        return value
    value = getpass.getpass("Вставьте SERVICE_API_TOKEN из Render: ").strip()
    if not value:
        raise RuntimeError("SERVICE_API_TOKEN не введён")
    return value


def _post_json(url: str, token: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CRM returned HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"CRM is unavailable: {exc}") from exc


async def _claim(api_url: str, token: str, agent_id: str) -> dict | None:
    response = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/kaspi-competitor-agent/claim",
        token,
        {
            "agent_id": agent_id,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "version": VERSION,
        },
    )
    return response.get("job")


async def _process_job(api_url: str, token: str, job: dict) -> None:
    print(f"Проверяю Kaspi: #{job['id']} {job['name']}")
    product = SimpleNamespace(
        id=job["product_id"],
        name=job["name"],
        brand=job.get("brand"),
        kaspi_product_id=job["kaspi_product_id"],
        merchant_sku=job.get("merchant_sku"),
    )
    try:
        market = await scan_kaspi_competitors(
            product,
            own_merchant_id=str(job["own_merchant_id"]),
            city_id=str(job["city_id"]),
            zone_id=str(job["zone_id"]),
        )
        payload = {
            "lease_token": job["lease_token"],
            "status": "succeeded",
            "own_price_kzt": None if market.own_price_kzt is None else str(market.own_price_kzt),
            "competitor_price_kzt": None if market.competitor_price_kzt is None else str(market.competitor_price_kzt),
            "competitor_name": market.competitor_name,
            "own_position": market.own_position,
            "seller_count": market.seller_count,
            "product_url": market.product_url,
        }
    except Exception as exc:
        payload = {
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": exc.__class__.__name__,
            "error_message": str(exc)[:4000],
        }
    result = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/kaspi-competitor-agent/jobs/{job['id']}/complete",
        token,
        payload,
    )
    print(f"Задание #{job['id']}: {result.get('status')}")


async def main(*, once: bool = False) -> int:
    api_url = (os.getenv("CRM_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    token = _service_token()
    agent_id = (os.getenv("KASPI_COMPETITOR_AGENT_ID") or f"kaspi-competitor-{socket.gethostname()}").strip()
    poll_seconds = max(1.0, float(os.getenv("KASPI_COMPETITOR_POLL_SECONDS") or "3"))
    concurrency = max(1, min(8, int(os.getenv("KASPI_COMPETITOR_CONCURRENCY") or "2")))

    print(f"LEO Kaspi Competitor Agent {VERSION}")
    print(f"CRM: {api_url}")
    print(f"Agent ID: {agent_id}")
    print(f"Параллельных проверок: {1 if once else concurrency}")
    print("Browser Agent поставщиков не используется.")

    semaphore = asyncio.Semaphore(1 if once else concurrency)

    async def worker(number: int) -> None:
        while True:
            try:
                job = await _claim(api_url, token, f"{agent_id}-w{number}")
                if not job:
                    if once:
                        return
                    await asyncio.sleep(poll_seconds)
                    continue
                async with semaphore:
                    await _process_job(api_url, token, job)
            except Exception as exc:
                print(f"Worker {number}: {exc}")
                if once:
                    return
                await asyncio.sleep(poll_seconds)

    if once:
        await worker(1)
        return 0
    await asyncio.gather(*(worker(number) for number in range(1, concurrency + 1)))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LEO local Kaspi competitor agent")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    started = time.time()
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(main(once=args.once)))
    except KeyboardInterrupt:
        print(f"Агент остановлен через {int(time.time() - started)} сек.")
