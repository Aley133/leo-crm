from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import getpass
import json
import os
import platform
import random
import socket
import sys
import time
import traceback
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.kaspi_fast_dumping_scanner import (
    KaspiCompetitorSnapshot,
    scan_kaspi_competitors,
)
from tools.kaspi_fast_dumping_session import KaspiMerchantSession


VERSION = "1.0.2"
DEFAULT_API_URL = "https://leo-crm-api.onrender.com"
HEARTBEAT_SECONDS = 20
IDLE_POLL_MAX_SECONDS = 15
CRM_HTTP_TIMEOUT_SECONDS = 30
CRM_RETRY_ATTEMPTS = 4
VERIFY_TIMEOUT_SECONDS = 120
VERIFY_POLL_SECONDS = 6
SCAN_TIMEOUT_SECONDS = 120
TRANSIENT_HTTP_STATUSES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
    # Cloudflare returns these non-standard statuses when the Render origin is
    # overloaded or temporarily unavailable. They must share the same backoff
    # as ordinary gateway failures instead of starting a new worker loop.
    520,
    521,
    522,
    523,
    524,
    530,
}
CRM_BACKOFF_MAX_SECONDS = 60.0
_WRITE_LOCK = asyncio.Lock()
_CRM_REQUEST_LOCK = asyncio.Lock()
_RUNTIME_SID: dict[int, str] = {}
_CRM_GATE_LOCK = Lock()
_CRM_FAILURE_COUNT = 0
_CRM_RETRY_NOT_BEFORE = 0.0
_INSTANCE_MUTEX_HANDLE: int | None = None


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


def _acquire_single_instance(workspace_id: int) -> None:
    """Keep one Windows Fast Agent process per workspace."""

    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt" or _INSTANCE_MUTEX_HANDLE is not None:
        return
    kernel32 = ctypes.windll.kernel32
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    mutex_name = f"Local\\LEO-Kaspi-Fast-Dumping-Agent-workspace-{workspace_id}"
    handle = create_mutex(None, False, mutex_name)
    if not handle:
        raise ctypes.WinError()
    error_code = int(kernel32.GetLastError())
    if error_code == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise RuntimeError(
            f"Fast Dumping Agent для workspace {workspace_id} уже запущен. "
            "Используйте одно окно агента."
        )
    _INSTANCE_MUTEX_HANDLE = int(handle)


def _crm_gate_delay(*, now: float | None = None) -> float:
    checked_at = time.monotonic() if now is None else now
    with _CRM_GATE_LOCK:
        return max(0.0, _CRM_RETRY_NOT_BEFORE - checked_at)


async def _wait_for_crm_gate() -> None:
    while True:
        delay = _crm_gate_delay()
        if delay <= 0:
            return
        await asyncio.sleep(delay)


def _record_crm_failure(
    *,
    retry_after: float | None,
    now: float | None = None,
    jitter: float | None = None,
) -> float:
    """Open one shared retry gate for heartbeat and every worker.

    A failing origin previously caused each coroutine to retry independently.
    The shared gate turns that fan-out into one bounded reconnect stream.
    """

    global _CRM_FAILURE_COUNT, _CRM_RETRY_NOT_BEFORE
    checked_at = time.monotonic() if now is None else now
    with _CRM_GATE_LOCK:
        _CRM_FAILURE_COUNT = min(10, _CRM_FAILURE_COUNT + 1)
        if retry_after is None:
            base = min(
                CRM_BACKOFF_MAX_SECONDS,
                float(2 ** (_CRM_FAILURE_COUNT - 1)),
            )
        else:
            base = min(CRM_BACKOFF_MAX_SECONDS, max(1.0, float(retry_after)))
        spread = (
            random.uniform(0.0, min(1.0, base * 0.2))
            if jitter is None
            else max(0.0, float(jitter))
        )
        delay = min(CRM_BACKOFF_MAX_SECONDS, base + spread)
        _CRM_RETRY_NOT_BEFORE = max(_CRM_RETRY_NOT_BEFORE, checked_at + delay)
        return delay


def _record_crm_success() -> None:
    global _CRM_FAILURE_COUNT, _CRM_RETRY_NOT_BEFORE
    with _CRM_GATE_LOCK:
        _CRM_FAILURE_COUNT = 0
        _CRM_RETRY_NOT_BEFORE = 0.0


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect_secret(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure credential storage is available on Windows only")
    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        ctypes.c_wchar_p("LEO Fast Dumping Agent"),
        None,
        None,
        None,
        1,
        ctypes.byref(target),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _unprotect_secret(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure credential storage is available on Windows only")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        1,
        ctypes.byref(target),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _app_dir() -> Path:
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _config_path(workspace_id: int) -> Path:
    return _app_dir() / f"kaspi_fast_dumping_agent.workspace-{workspace_id}.json"


def _log_path(workspace_id: int | None = None) -> Path:
    suffix = f".workspace-{workspace_id}" if workspace_id else ""
    return _app_dir() / f"kaspi_fast_dumping_agent{suffix}.log"


def _log(message: str, *, workspace_id: int | None = None) -> None:
    timestamp = datetime.now(UTC).isoformat()
    try:
        with _log_path(workspace_id).open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass
    print(message, flush=True)


def _load_config(workspace_id: int) -> dict:
    path = _config_path(workspace_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(config: dict, workspace_id: int) -> None:
    allowed = {
        "api_url",
        "workspace_id",
        "agent_id",
        "concurrency",
        "merchant_uid",
        "store_id",
        "email",
        "service_token_dpapi",
        "password_dpapi",
        "mc_sid_dpapi",
    }
    payload = {key: value for key, value in config.items() if key in allowed}
    _config_path(workspace_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prompt_text(title: str, prompt: str, *, secret: bool = False) -> str:
    if os.name == "nt":
        try:
            from tkinter import Tk, simpledialog

            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            value = simpledialog.askstring(
                title,
                prompt,
                show="*" if secret else None,
                parent=root,
            )
            root.destroy()
            return (value or "").strip()
        except Exception:
            pass
    try:
        return (
            getpass.getpass(f"{prompt}: ").strip()
            if secret
            else input(f"{prompt}: ").strip()
        )
    except (EOFError, KeyboardInterrupt):
        return ""


def _show_message(title: str, message: str, *, error: bool = False) -> None:
    if os.name == "nt":
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
            return
        except Exception:
            pass
    print(f"{title}: {message}", flush=True)


def _workspace_id(requested: int | None) -> int:
    raw: object | None = requested or os.getenv("KASPI_FAST_DUMPING_WORKSPACE_ID")
    if raw in (None, ""):
        raw = _prompt_text(
            "LEO Fast Dumping Agent",
            "ID аккаунта CRM (1 — BARWORK, 3 — LeoXpress)",
        )
    try:
        value = int(raw or 1)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Workspace ID должен быть целым числом") from exc
    if value < 1:
        raise RuntimeError("Workspace ID должен быть больше нуля")
    return value


def _plain_setting(
    config: dict,
    *,
    key: str,
    env_name: str,
    prompt: str,
    reconfigure: bool,
) -> str:
    value = (os.getenv(env_name) or (None if reconfigure else config.get(key)) or "").strip()
    if not value:
        value = _prompt_text("LEO Fast Dumping Agent", prompt)
    if not value:
        raise RuntimeError(f"Не заполнено: {prompt}")
    config[key] = value
    return value


def _secret_setting(
    config: dict,
    *,
    key: str,
    env_name: str,
    prompt: str,
    reconfigure: bool,
) -> str:
    value = (os.getenv(env_name) or "").strip()
    encrypted_key = f"{key}_dpapi"
    if not value and not reconfigure and os.name == "nt" and config.get(encrypted_key):
        try:
            value = _unprotect_secret(str(config[encrypted_key]))
        except Exception:
            value = ""
    if not value:
        value = _prompt_text("LEO Fast Dumping Agent", prompt, secret=True)
    if not value:
        raise RuntimeError(f"Не заполнено: {prompt}")
    if os.name == "nt":
        config[encrypted_key] = _protect_secret(value)
    return value


def _load_sid(config: dict, workspace_id: int) -> str | None:
    if os.name == "nt" and config.get("mc_sid_dpapi"):
        try:
            return _unprotect_secret(str(config["mc_sid_dpapi"]))
        except Exception:
            return None
    return _RUNTIME_SID.get(workspace_id)


def _save_sid(config: dict, workspace_id: int, sid: str) -> None:
    if os.name == "nt":
        config["mc_sid_dpapi"] = _protect_secret(sid)
        _save_config(config, workspace_id)
    else:
        _RUNTIME_SID[workspace_id] = sid


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
        with urlopen(request, timeout=CRM_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retry_after = None
        try:
            raw = exc.headers.get("Retry-After") if exc.headers else None
            retry_after = float(raw) if raw else None
        except (TypeError, ValueError):
            retry_after = None
        raise CRMRequestError(
            f"CRM returned HTTP {exc.code}: {body}",
            retryable=exc.code in TRANSIENT_HTTP_STATUSES,
            retry_after=retry_after,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise CRMRequestError(
            f"CRM is unavailable: {exc}",
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
        await _wait_for_crm_gate()
        try:
            async with _CRM_REQUEST_LOCK:
                # Another request may have opened the shared circuit while this
                # coroutine was waiting for the single HTTP slot.
                await _wait_for_crm_gate()
                result = await asyncio.to_thread(_post_json, url, token, payload)
        except CRMRequestError as exc:
            if not exc.retryable:
                raise
            delay = _record_crm_failure(retry_after=exc.retry_after)
            if attempt >= CRM_RETRY_ATTEMPTS:
                raise
            _log(
                f"{operation}: временная ошибка CRM; общий повтор через {delay:.1f} с"
            )
            continue
        _record_crm_success()
        return result
    raise RuntimeError(f"{operation}: retry loop finished unexpectedly")


def _agent_payload(
    agent_id: str,
    workspace_id: int,
    concurrency: int,
    merchant_uid: str,
) -> dict:
    return {
        "agent_id": agent_id,
        "workspace_id": workspace_id,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "version": VERSION,
        "concurrency": concurrency,
        "merchant_uid": merchant_uid,
    }


def _configured_concurrency() -> int:
    raw = (os.getenv("KASPI_FAST_DUMPING_CONCURRENCY") or "1").strip()
    try:
        requested = int(raw)
    except ValueError:
        requested = 1
    return max(1, min(2, requested))


def _market_payload(market: KaspiCompetitorSnapshot) -> dict:
    def money(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    return {
        "product_name": market.product_name,
        "product_brand": market.product_brand,
        "own_price_kzt": money(market.own_price_kzt),
        "competitor_price_kzt": money(market.competitor_price_kzt),
        "competitor_name": market.competitor_name,
        "own_position": market.own_position,
        "seller_count": market.seller_count,
        "product_url": market.product_url,
        "own_delivery": market.own_delivery,
        "competitor_delivery": market.competitor_delivery,
        "offers": list(market.offers),
        "page_visible_price_kzt": money(market.page_visible_price_kzt),
        "market_context_ok": market.market_context_ok,
        "market_context_reason": market.market_context_reason,
    }


async def _scan(job: dict, merchant_uid: str) -> KaspiCompetitorSnapshot:
    kaspi_product_id = str(job.get("kaspi_product_id") or "").strip()
    if not kaspi_product_id:
        raise ValueError("Kaspi product id is missing from Fast Dumping job")
    async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
        return await scan_kaspi_competitors(
            kaspi_product_id=kaspi_product_id,
            own_merchant_id=merchant_uid,
            own_merchant_sku=(
                str(job.get("merchant_sku") or "").strip() or None
            ),
            city_id=str(job["city_id"]),
            zone_id=str(job["zone_id"]),
            product_name_hint=str(job.get("name") or "") or None,
            product_brand_hint=str(job.get("brand") or "") or None,
        )


async def _process_scan(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
) -> None:
    _log(f"Сканирование #{job['id']}: {job['name']}", workspace_id=workspace_id)
    try:
        market = await _scan(job, merchant_uid)
        payload = {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "status": "succeeded",
            "market": _market_payload(market),
        }
    except Exception as exc:
        payload = {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:4000],
        }
    result = await _post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/scan-complete",
        token,
        payload,
        operation=f"Сохранение сканирования #{job['id']}",
    )
    _log(
        f"Сканирование #{job['id']}: {result.get('status')}",
        workspace_id=workspace_id,
    )


async def _verify_price(
    *,
    job: dict,
    merchant_uid: str,
    target: Decimal,
) -> tuple[bool, Decimal | None, float]:
    started = time.monotonic()
    deadline = started + VERIFY_TIMEOUT_SECONDS
    observed: Decimal | None = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(VERIFY_POLL_SECONDS, max(0.0, remaining)))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                market = await _scan(job, merchant_uid)
        except TimeoutError:
            break
        observed = market.own_price_kzt
        if observed == target:
            return True, observed, round(time.monotonic() - started, 1)
    return False, observed, round(time.monotonic() - started, 1)


async def _process_apply(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
    store_id: str,
    merchant_session: KaspiMerchantSession,
) -> None:
    identity = {
        "agent_id": agent_id,
        "workspace_id": workspace_id,
        "lease_token": job["lease_token"],
    }
    prepared = await _post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/prepare-apply",
        token,
        identity,
        operation=f"Проверка решения #{job['id']}",
    )
    if not prepared.get("ready"):
        _log(
            f"Запись #{job['id']} отменена CRM: {prepared.get('reason')}",
            workspace_id=workspace_id,
        )
        return

    target = Decimal(str(prepared["target_price_kzt"]))
    write_result: dict = {}
    refreshed = False
    try:
        async with _WRITE_LOCK:
            sid, refreshed = await asyncio.to_thread(
                merchant_session.ensure_valid_sid
            )
            write_result = await asyncio.to_thread(
                merchant_session.write_price,
                mc_sid=sid,
                store_id=store_id,
                city_id=str(prepared["city_id"]),
                sku=str(prepared["sku"]),
                model=str(prepared["model"]),
                stock_count=int(prepared["stock_count"]),
                price=int(target),
            )
            if write_result.get("status_code") in (401, 403):
                sid, _ = await asyncio.to_thread(
                    merchant_session.ensure_valid_sid,
                    force_refresh=True,
                )
                refreshed = True
                write_result = await asyncio.to_thread(
                    merchant_session.write_price,
                    mc_sid=sid,
                    store_id=store_id,
                    city_id=str(prepared["city_id"]),
                    sku=str(prepared["sku"]),
                    model=str(prepared["model"]),
                    stock_count=int(prepared["stock_count"]),
                    price=int(target),
                )
        verified = False
        observed = None
        latency = 0.0
        if write_result.get("accepted"):
            verify_job = {
                **job,
                "merchant_sku": prepared["sku"],
                "city_id": prepared["city_id"],
                "zone_id": prepared["zone_id"],
                "name": prepared["model"],
            }
            verified, observed, latency = await _verify_price(
                job=verify_job,
                merchant_uid=merchant_uid,
                target=target,
            )
        payload = {
            **identity,
            "accepted": bool(write_result.get("accepted")),
            "verified": verified,
            "status_code": write_result.get("status_code"),
            "operation_id": write_result.get("operation_id"),
            "latency_seconds": latency,
            "observed_own_price_kzt": (
                None if observed is None else format(observed, "f")
            ),
            "session_refreshed": refreshed,
            "error_code": (
                None
                if write_result.get("accepted")
                else "merchant_write_failed"
            ),
            "error_message": write_result.get("error_message"),
        }
    except Exception as exc:
        payload = {
            **identity,
            "accepted": False,
            "verified": False,
            "session_refreshed": refreshed,
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:2000],
        }
    result = await _post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/apply-complete",
        token,
        payload,
        operation=f"Подтверждение записи #{job['id']}",
    )
    _log(
        f"Запись #{job['id']}: {result.get('status')}",
        workspace_id=workspace_id,
    )


async def _process_verify(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
) -> None:
    observed = None
    try:
        market = await _scan(job, merchant_uid)
        observed = market.own_price_kzt
    except Exception as exc:
        _log(f"Проверка #{job['id']}: {exc}", workspace_id=workspace_id)
    await _post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/verify-complete",
        token,
        {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "observed_own_price_kzt": (
                None if observed is None else format(observed, "f")
            ),
        },
        operation=f"Сохранение проверки #{job['id']}",
    )


async def main(
    *,
    once: bool = False,
    workspace_id: int | None = None,
    reconfigure: bool = False,
) -> int:
    selected_workspace = _workspace_id(workspace_id)
    _acquire_single_instance(selected_workspace)
    config = _load_config(selected_workspace)
    api_url = (
        os.getenv("CRM_API_URL")
        or config.get("api_url")
        or DEFAULT_API_URL
    ).strip().rstrip("/")
    token = _secret_setting(
        config,
        key="service_token",
        env_name="CRM_SERVICE_TOKEN",
        prompt="SERVICE_API_TOKEN из Render",
        reconfigure=reconfigure,
    )
    previous_merchant_uid = str(config.get("merchant_uid") or "").strip()
    merchant_uid = _plain_setting(
        config,
        key="merchant_uid",
        env_name="KASPI_MERCHANT_UID",
        prompt="Kaspi Merchant UID",
        reconfigure=reconfigure,
    )
    if reconfigure or (
        previous_merchant_uid and previous_merchant_uid != merchant_uid
    ):
        config.pop("mc_sid_dpapi", None)
        _RUNTIME_SID.pop(selected_workspace, None)
    store_id = _plain_setting(
        config,
        key="store_id",
        env_name="KASPI_STORE_ID",
        prompt="Kaspi Store ID",
        reconfigure=reconfigure,
    )
    email = _plain_setting(
        config,
        key="email",
        env_name="KASPI_LOGIN_EMAIL",
        prompt="Email Merchant Cabinet",
        reconfigure=reconfigure,
    )
    password = _secret_setting(
        config,
        key="password",
        env_name="KASPI_LOGIN_PASSWORD",
        prompt="Пароль Merchant Cabinet",
        reconfigure=reconfigure,
    )
    concurrency = _configured_concurrency()
    agent_id = (
        os.getenv("KASPI_FAST_DUMPING_AGENT_ID")
        or config.get("agent_id")
        or f"kaspi-fast-dumping-{socket.gethostname()}-workspace-{selected_workspace}"
    ).strip()
    config.update(
        {
            "api_url": api_url,
            "workspace_id": selected_workspace,
            "agent_id": agent_id,
            "concurrency": concurrency,
        }
    )
    _save_config(config, selected_workspace)
    merchant_session = KaspiMerchantSession(
        merchant_uid=merchant_uid,
        email=email,
        password=password,
        load_sid=lambda: _load_sid(config, selected_workspace),
        save_sid=lambda sid: _save_sid(config, selected_workspace, sid),
    )
    base_identity = _agent_payload(
        agent_id,
        selected_workspace,
        concurrency,
        merchant_uid,
    )

    await _post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/heartbeat",
        token,
        {**base_identity, "status": "online"},
        operation="Подключение к CRM",
    )
    _log(
        f"LEO Kaspi Fast Dumping Agent {VERSION} · workspace {selected_workspace}",
        workspace_id=selected_workspace,
    )
    _log(
        "Realtime-записи изолированы от обычного демпинга и XML.",
        workspace_id=selected_workspace,
    )
    if os.name == "nt" and not once:
        _show_message(
            "LEO Fast Dumping Agent",
            "Агент подключён и работает. Не закрывайте это окно.",
        )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await _post_json_with_retry(
                    f"{api_url}/api/fast-dumping-agent/heartbeat",
                    token,
                    {**base_identity, "status": "online"},
                    operation="Heartbeat",
                )
            except Exception as exc:
                _log(f"Heartbeat: {exc}", workspace_id=selected_workspace)

    async def worker(number: int) -> None:
        idle_seconds = 2.0
        worker_id = f"{agent_id}-w{number}"
        while True:
            try:
                claim = await _post_json_with_retry(
                    f"{api_url}/api/fast-dumping-agent/claim",
                    token,
                    _agent_payload(
                        worker_id,
                        selected_workspace,
                        concurrency,
                        merchant_uid,
                    ),
                    operation="Получение задания",
                )
                job = claim.get("job")
                if not job:
                    if once:
                        return
                    idle_seconds = min(
                        IDLE_POLL_MAX_SECONDS,
                        max(
                            2.0,
                            float(claim.get("retry_after_seconds") or 2),
                            idle_seconds * 1.5,
                        ),
                    )
                    await asyncio.sleep(idle_seconds)
                    continue
                idle_seconds = 2.0
                common = {
                    "api_url": api_url,
                    "token": token,
                    "job": job,
                    "agent_id": worker_id,
                    "workspace_id": selected_workspace,
                    "merchant_uid": merchant_uid,
                }
                if job["stage"] == "scan":
                    await _process_scan(**common)
                elif job["stage"] == "apply":
                    await _process_apply(
                        **common,
                        store_id=store_id,
                        merchant_session=merchant_session,
                    )
                else:
                    await _process_verify(**common)
            except Exception as exc:
                _log(f"Worker {number}: {exc}", workspace_id=selected_workspace)
                if once:
                    return
                await asyncio.sleep(3)

    if once:
        await worker(1)
        return 0
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await asyncio.gather(
            *(worker(number) for number in range(1, concurrency + 1))
        )
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LEO local Kaspi Fast Dumping Agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--workspace-id", type=int)
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Ask for Merchant Cabinet settings again",
    )
    return parser.parse_args()


if __name__ == "__main__":
    started = time.time()
    args = _parse_args()
    try:
        raise SystemExit(
            asyncio.run(
                main(
                    once=args.once,
                    workspace_id=args.workspace_id,
                    reconfigure=args.reconfigure,
                )
            )
        )
    except KeyboardInterrupt:
        _log(f"Агент остановлен через {int(time.time() - started)} сек.")
    except Exception as exc:
        details = "".join(traceback.format_exception(exc)).strip()
        _log(details)
        _show_message(
            "LEO Fast Dumping Agent — ошибка",
            f"{exc}\n\nПодробности: {_log_path(args.workspace_id)}",
            error=True,
        )
        if sys.stdin and sys.stdin.isatty():
            try:
                input("Нажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
        raise SystemExit(1)
