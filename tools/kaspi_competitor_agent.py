from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import platform
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.kaspi_offer_competitor import scan_kaspi_competitors

VERSION = "1.1.0"
DEFAULT_API_URL = "https://leo-crm-api.onrender.com"
HEARTBEAT_SECONDS = 15


def _config_path() -> Path:
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    root.mkdir(parents=True, exist_ok=True)
    return root / "kaspi_competitor_agent.json"


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(payload: dict) -> None:
    path = _config_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _service_token(config: dict) -> str:
    value = (os.getenv("CRM_SERVICE_TOKEN") or config.get("service_token") or "").strip()
    if value:
        return value
    value = getpass.getpass("Вставьте SERVICE_API_TOKEN из Render: ").strip()
    if not value:
        raise RuntimeError("SERVICE_API_TOKEN не введён")
    config["service_token"] = value
    _save_config(config)
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


def _agent_payload(agent_id: str, concurrency: int) -> dict:
    return {
        "agent_id": agent_id,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "version": VERSION,
        "concurrency": concurrency,
    }


async def _heartbeat(api_url: str, token: str, agent_id: str, concurrency: int) -> None:
    payload = {**_agent_payload(agent_id, concurrency), "status": "online"}
    while True:
        try:
            await asyncio.to_thread(
                _post_json,
                f"{api_url}/api/kaspi-competitor-agent/heartbeat",
                token,
                payload,
            )
        except Exception as exc:
            print(f"Heartbeat: {exc}")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def _claim(api_url: str, token: str, agent_id: str, concurrency: int) -> dict | None:
    response = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/kaspi-competitor-agent/claim",
        token,
        _agent_payload(agent_id, concurrency),
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
    config = _load_config()
    api_url = (os.getenv("CRM_API_URL") or config.get("api_url") or DEFAULT_API_URL).strip().rstrip("/")
    token = _service_token(config)
    agent_id = (os.getenv("KASPI_COMPETITOR_AGENT_ID") or config.get("agent_id") or f"kaspi-competitor-{socket.gethostname()}").strip()
    poll_seconds = max(1.0, float(os.getenv("KASPI_COMPETITOR_POLL_SECONDS") or "3"))
    concurrency = max(1, min(8, int(os.getenv("KASPI_COMPETITOR_CONCURRENCY") or config.get("concurrency") or "2")))
    config.update({"api_url": api_url, "agent_id": agent_id, "concurrency": concurrency})
    _save_config(config)

    print(f"LEO Kaspi Competitor Agent {VERSION}")
    print(f"CRM: {api_url}")
    print(f"Agent ID: {agent_id}")
    print(f"Параллельных проверок: {1 if once else concurrency}")
    print("Browser Agent поставщиков не используется.")

    semaphore = asyncio.Semaphore(1 if once else concurrency)

    async def worker(number: int) -> None:
        while True:
            try:
                job = await _claim(api_url, token, f"{agent_id}-w{number}", 1 if once else concurrency)
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

    heartbeat_task = asyncio.create_task(_heartbeat(api_url, token, agent_id, concurrency))
    try:
        await asyncio.gather(*(worker(number) for number in range(1, concurrency + 1)))
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
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
