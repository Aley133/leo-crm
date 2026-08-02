from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import platform
import socket
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.kaspi_competitor_scanner import scan_kaspi_competitors

VERSION = "1.4.0"
DEFAULT_API_URL = "https://leo-crm-api.onrender.com"
HEARTBEAT_SECONDS = 15
CRM_HTTP_TIMEOUT_SECONDS = 30
CRM_RETRY_ATTEMPTS = 4
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 502, 503, 504}


class CRMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _app_dir() -> Path:
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _config_path() -> Path:
    return _app_dir() / "kaspi_competitor_agent.json"


def _log_path() -> Path:
    return _app_dir() / "kaspi_competitor_agent.log"


def _log(message: str) -> None:
    timestamp = datetime.now(UTC).isoformat()
    try:
        with _log_path().open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass
    print(message, flush=True)


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
    _config_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _prompt_token_gui() -> str:
    try:
        from tkinter import Tk, simpledialog

        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        value = simpledialog.askstring(
            "LEO Kaspi Competitor Agent",
            "Вставьте SERVICE_API_TOKEN из Render:",
            show="*",
            parent=root,
        )
        root.destroy()
        return (value or "").strip()
    except Exception:
        return ""


def _show_message(title: str, message: str, *, error: bool = False) -> None:
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if error:
            messagebox.showerror(title, message, parent=root)
        else:
            messagebox.showinfo(title, message, parent=root)
        root.destroy()
    except Exception:
        _log(f"{title}: {message}")


def _service_token(config: dict) -> str:
    value = (os.getenv("CRM_SERVICE_TOKEN") or config.get("service_token") or "").strip()
    if value:
        return value
    value = _prompt_token_gui() if os.name == "nt" else ""
    if not value:
        try:
            value = getpass.getpass("Вставьте SERVICE_API_TOKEN из Render: ").strip()
        except (EOFError, KeyboardInterrupt):
            value = ""
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
        with urlopen(request, timeout=CRM_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retry_after = None
        try:
            raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
            retry_after = float(raw_retry_after) if raw_retry_after else None
        except (TypeError, ValueError):
            retry_after = None
        raise CRMRequestError(
            f"CRM returned HTTP {exc.code}: {body}",
            retryable=exc.code in TRANSIENT_HTTP_STATUSES,
            retry_after=retry_after,
        ) from exc
    except URLError as exc:
        raise CRMRequestError(
            f"CRM is unavailable: {exc}",
            retryable=True,
        ) from exc
    except TimeoutError as exc:
        raise CRMRequestError(
            f"CRM request timed out after {CRM_HTTP_TIMEOUT_SECONDS} seconds",
            retryable=True,
        ) from exc


async def _post_json_with_retry(
    url: str,
    token: str,
    payload: dict,
    *,
    operation: str,
) -> dict:
    for attempt in range(1, CRM_RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(_post_json, url, token, payload)
        except CRMRequestError as exc:
            if not exc.retryable or attempt >= CRM_RETRY_ATTEMPTS:
                raise
            delay = exc.retry_after if exc.retry_after is not None else 2 ** (attempt - 1)
            delay = min(8.0, max(1.0, delay))
            _log(
                f"{operation}: CRM временно занята; повтор "
                f"{attempt + 1}/{CRM_RETRY_ATTEMPTS} через {delay:g} с"
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{operation}: retry loop finished unexpectedly")


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
            await _post_json_with_retry(
                f"{api_url}/api/kaspi-competitor-agent/heartbeat",
                token,
                payload,
                operation="Heartbeat",
            )
        except Exception as exc:
            _log(f"Heartbeat: {exc}")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def _claim(api_url: str, token: str, agent_id: str, concurrency: int) -> dict | None:
    response = await _post_json_with_retry(
        f"{api_url}/api/kaspi-competitor-agent/claim",
        token,
        _agent_payload(agent_id, concurrency),
        operation="Получение задания",
    )
    return response.get("job")


async def _process_job(api_url: str, token: str, job: dict) -> None:
    _log(f"Проверяю Kaspi: #{job['id']} {job['name']}")
    try:
        market = await scan_kaspi_competitors(
            product_name=str(job["name"]),
            product_brand=job.get("brand"),
            kaspi_product_id=str(job["kaspi_product_id"]),
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
    result = await _post_json_with_retry(
        f"{api_url}/api/kaspi-competitor-agent/jobs/{job['id']}/complete",
        token,
        payload,
        operation=f"Сохранение задания #{job['id']}",
    )
    decision = (result.get("result") or {}).get("decision") or {}
    if result.get("status") == "succeeded_local" and decision:
        _log(
            f"Задание #{job['id']}: успешно · склад "
            f"{int(decision.get('stock_count') or 0)} шт. · "
            f"XML {decision.get('target_price_kzt') or '—'} KZT · "
            f"preOrder {int(decision.get('preorder_days') or 0)} дн."
        )
    else:
        _log(f"Задание #{job['id']}: {result.get('status')}")


async def main(*, once: bool = False) -> int:
    config = _load_config()
    api_url = (os.getenv("CRM_API_URL") or config.get("api_url") or DEFAULT_API_URL).strip().rstrip("/")
    token = _service_token(config)
    agent_id = (
        os.getenv("KASPI_COMPETITOR_AGENT_ID")
        or config.get("agent_id")
        or f"kaspi-competitor-{socket.gethostname()}"
    ).strip()
    poll_seconds = max(1.0, float(os.getenv("KASPI_COMPETITOR_POLL_SECONDS") or "3"))
    concurrency = max(
        1,
        min(8, int(os.getenv("KASPI_COMPETITOR_CONCURRENCY") or config.get("concurrency") or "2")),
    )
    config.update({"api_url": api_url, "agent_id": agent_id, "concurrency": concurrency})
    _save_config(config)

    _log(f"LEO Kaspi Competitor Agent {VERSION}")
    _log(f"CRM: {api_url}")
    _log(f"Agent ID: {agent_id}")
    _log(f"Параллельных проверок: {1 if once else concurrency}")
    _log("Автономный режим: DATABASE_URL, SQLAlchemy и Browser Agent не используются.")

    try:
        await _post_json_with_retry(
            f"{api_url}/api/kaspi-competitor-agent/heartbeat",
            token,
            {**_agent_payload(agent_id, concurrency), "status": "online"},
            operation="Подключение к CRM",
        )
    except Exception as exc:
        raise RuntimeError(f"Не удалось подключиться к LEO CRM: {exc}") from exc

    if os.name == "nt" and not once:
        _show_message(
            "LEO Kaspi Competitor Agent",
            "Агент подключён к LEO CRM и работает. Не закрывайте это окно.",
        )

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
                _log(f"Worker {number}: {exc}")
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
        _log(f"Агент остановлен через {int(time.time() - started)} сек.")
    except Exception as exc:
        details = "".join(traceback.format_exception(exc)).strip()
        _log(details)
        _show_message(
            "LEO Kaspi Competitor Agent — ошибка",
            f"{exc}\n\nПодробности сохранены в:\n{_log_path()}",
            error=True,
        )
        if sys.stdin and sys.stdin.isatty():
            try:
                input("Нажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
        raise SystemExit(1)
