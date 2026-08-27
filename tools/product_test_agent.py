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
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.kaspi_fast_dumping_scanner import inspect_kaspi_product
from tools.kaspi_fast_dumping_session import KaspiMerchantSession
from tools.ozon_http import OzonSessionResolver
from tools.product_discovery.kaspi_offer_creator import MerchantOfferApi
from tools.product_discovery.runtime import discover_products, validate_supplier_url


VERSION = "1.0.2"
AGENT_KIND = "product_test"
DEFAULT_API_URL = "https://leo-crm-api.onrender.com"
HEARTBEAT_SECONDS = 20
IDLE_POLL_SECONDS = 1.0
CRM_HTTP_TIMEOUT_SECONDS = 30
CRM_RETRY_ATTEMPTS = 4
CRM_BACKOFF_MAX_SECONDS = 60.0
SCAN_TIMEOUT_SECONDS = 180
LONG_JOB_TIMEOUT_SECONDS = 1800
TRANSIENT_HTTP_STATUSES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
    530,
}

_CRM_REQUEST_LOCK = asyncio.Lock()
_CRM_GATE_LOCK = Lock()
_CRM_FAILURE_COUNT = 0
_CRM_RETRY_NOT_BEFORE = 0.0
_RUNTIME_SID: dict[int, str] = {}
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


class AgentReconfigureRequired(RuntimeError):
    """Stop the process after invalid saved credentials were cleared."""


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _app_dir() -> Path:
    root = Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _config_path(workspace_id: int) -> Path:
    return _app_dir() / f"product_test_agent.workspace-{workspace_id}.json"


def _legacy_fast_config_path(workspace_id: int) -> Path:
    return _app_dir() / f"kaspi_fast_dumping_agent.workspace-{workspace_id}.json"


def _log_path(workspace_id: int | None = None) -> Path:
    suffix = f".workspace-{workspace_id}" if workspace_id else ""
    return _app_dir() / f"product_test_agent{suffix}.log"


def _log(message: str, *, workspace_id: int | None = None) -> None:
    timestamp = datetime.now(UTC).isoformat()
    try:
        with _log_path(workspace_id).open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass
    print(message, flush=True)


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_config(workspace_id: int) -> dict:
    own = _read_config(_config_path(workspace_id))
    if own:
        return own
    # First launch is intentionally smooth: reuse the already encrypted Fast
    # Agent account settings, then persist an independent Product Test copy.
    return _read_config(_legacy_fast_config_path(workspace_id))


def _save_config(config: dict, workspace_id: int) -> None:
    allowed = {
        "api_url",
        "workspace_id",
        "agent_id",
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


def _protect_secret(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Secure credential storage is available on Windows only")
    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        ctypes.c_wchar_p("LEO Product Test Agent"),
        None,
        None,
        None,
        1,
        ctypes.byref(target),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(target.pbData, target.cbData)).decode("ascii")
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


def _prompt_text(prompt: str, *, secret: bool = False, multiline: bool = False) -> str:
    if os.name == "nt":
        try:
            from tkinter import Tk, simpledialog

            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            value = simpledialog.askstring(
                "LEO Product Test Agent",
                prompt,
                show="*" if secret and not multiline else None,
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
    raw: object | None = (
        requested
        or os.getenv("PRODUCT_TEST_WORKSPACE_ID")
        or os.getenv("KASPI_FAST_DUMPING_WORKSPACE_ID")
    )
    if raw in (None, ""):
        raw = _prompt_text("ID аккаунта CRM (1 — BARWORK, 3 — LeoXpress)")
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
    value = str(os.getenv(env_name) or (None if reconfigure else config.get(key)) or "").strip()
    if not value:
        value = _prompt_text(prompt)
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
    value = str(os.getenv(env_name) or "").strip()
    encrypted_key = f"{key}_dpapi"
    if not value and not reconfigure and os.name == "nt" and config.get(encrypted_key):
        try:
            value = _unprotect_secret(str(config[encrypted_key]))
        except Exception:
            value = ""
    if not value:
        value = _prompt_text(prompt, secret=True)
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


def _acquire_single_instance(workspace_id: int) -> None:
    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt" or _INSTANCE_MUTEX_HANDLE is not None:
        return
    kernel32 = ctypes.windll.kernel32
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(
        None,
        False,
        f"Local\\LEO-Product-Test-Agent-workspace-{workspace_id}",
    )
    if not handle:
        raise ctypes.WinError()
    if int(kernel32.GetLastError()) == 183:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise RuntimeError(
            f"Product Test Agent для workspace {workspace_id} уже запущен. "
            "Используйте одно окно агента."
        )
    _INSTANCE_MUTEX_HANDLE = int(handle)


def _ensure_ozon_session() -> None:
    resolver = OzonSessionResolver()
    try:
        resolver.resolve(validate=True)
        return
    except Exception:
        pass
    curl_text = _prompt_text(
        "Ozon HTTP-сессия не найдена. Вставьте Copy as cURL (bash) любого "
        "Network-запроса выдачи /search/ Ozon. Cookies останутся зашифрованы "
        "на этом компьютере.",
        multiline=True,
    )
    if not curl_text:
        raise RuntimeError("Ozon HTTP session не настроена")
    resolver.import_curl(curl_text, validate=True)


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
        raw_retry = exc.headers.get("Retry-After") if exc.headers else None
        try:
            retry_after = float(raw_retry) if raw_retry else None
        except (TypeError, ValueError):
            retry_after = None
        raise CRMRequestError(
            f"CRM returned HTTP {exc.code}: {body}",
            retryable=exc.code in TRANSIENT_HTTP_STATUSES,
            retry_after=retry_after,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise CRMRequestError(f"CRM is unavailable: {exc}", retryable=True) from exc


def _crm_gate_delay() -> float:
    with _CRM_GATE_LOCK:
        return max(0.0, _CRM_RETRY_NOT_BEFORE - time.monotonic())


def _record_crm_failure(retry_after: float | None) -> float:
    global _CRM_FAILURE_COUNT, _CRM_RETRY_NOT_BEFORE
    with _CRM_GATE_LOCK:
        _CRM_FAILURE_COUNT = min(10, _CRM_FAILURE_COUNT + 1)
        base = (
            min(CRM_BACKOFF_MAX_SECONDS, max(1.0, retry_after))
            if retry_after is not None
            else min(CRM_BACKOFF_MAX_SECONDS, float(2 ** (_CRM_FAILURE_COUNT - 1)))
        )
        delay = min(CRM_BACKOFF_MAX_SECONDS, base + random.uniform(0.0, min(1.0, base * 0.2)))
        _CRM_RETRY_NOT_BEFORE = max(_CRM_RETRY_NOT_BEFORE, time.monotonic() + delay)
        return delay


def _record_crm_success() -> None:
    global _CRM_FAILURE_COUNT, _CRM_RETRY_NOT_BEFORE
    with _CRM_GATE_LOCK:
        _CRM_FAILURE_COUNT = 0
        _CRM_RETRY_NOT_BEFORE = 0.0


async def _post_json_with_retry(
    url: str,
    token: str,
    payload: dict,
    *,
    operation: str,
) -> dict:
    async with _CRM_REQUEST_LOCK:
        last_error: Exception | None = None
        for attempt in range(CRM_RETRY_ATTEMPTS):
            delay = _crm_gate_delay()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await asyncio.to_thread(_post_json, url, token, payload)
                _record_crm_success()
                return result
            except CRMRequestError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= CRM_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_record_crm_failure(exc.retry_after))
        raise RuntimeError(f"{operation}: {last_error}")


def _agent_payload(agent_id: str, workspace_id: int, merchant_uid: str) -> dict:
    return {
        "agent_id": agent_id,
        "agent_kind": AGENT_KIND,
        "workspace_id": workspace_id,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "version": VERSION,
        "concurrency": 1,
        "merchant_uid": merchant_uid,
    }


def _is_rate_limited(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return "429" in text or "too many requests" in text or "rate limit" in text


async def _execute_job(
    job: dict,
    *,
    merchant_session: KaspiMerchantSession,
    store_id: str,
) -> dict:
    job_type = str(job.get("job_type") or "inspect")
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    if job_type == "discover":
        merchant_catalog = MerchantOfferApi(
            merchant_session,
            store_id=store_id,
            city_id=str(job.get("city_id") or "196220100"),
        )
        return await asyncio.to_thread(
            discover_products,
            query=str(job.get("reference") or ""),
            city_id=str(job.get("city_id") or "196220100"),
            target_new=int(options.get("target_new") or 10),
            max_kaspi_scan=int(options.get("max_kaspi_scan") or 200),
            max_ozon_queries=int(options.get("max_ozon_queries") or 3),
            image_verify=bool(options.get("image_verify", True)),
            existing_kaspi_ids={str(value) for value in options.get("existing_kaspi_ids") or []},
            merchant_catalog=merchant_catalog,
        )
    if job_type == "validate_supplier":
        return await asyncio.to_thread(
            validate_supplier_url,
            str(options.get("supplier_url") or ""),
            product=options.get("product") if isinstance(options.get("product"), dict) else None,
        )
    if job_type == "create_offer":
        creator = MerchantOfferApi(
            merchant_session,
            store_id=store_id,
            city_id=str(job.get("city_id") or "196220100"),
        )
        result = await asyncio.to_thread(
            creator.create_linked_offer,
            master_sku=str(options["master_sku"]),
            model=str(options["model"]),
            price=int(options["initial_price_kzt"]),
            stock=int(options["stock_count"]),
            preorder=int(options["preorder_days"]),
            live=True,
            attempts=60,
            poll_seconds=2.0,
        )
        if result.get("result") not in {"CREATED_AND_VISIBLE", "ALREADY_EXISTS"}:
            raise RuntimeError(str(result.get("result") or "Kaspi offer was not confirmed"))
        after = result.get("after") or result.get("before") or {}
        if not after.get("found") or not after.get("price_kzt"):
            raise RuntimeError("Kaspi принял создание, но оффер с ценой ещё не появился")
        return result
    return await inspect_kaspi_product(
        reference=str(job.get("reference") or ""),
        city_id=str(job.get("city_id") or "196220100"),
        zone_id=str(job.get("zone_id") or "Magnum_ZONE1"),
    )


async def _run_job_with_retry(
    job: dict,
    *,
    merchant_session: KaspiMerchantSession,
    store_id: str,
) -> dict:
    job_type = str(job.get("job_type") or "inspect")
    timeout_seconds = (
        LONG_JOB_TIMEOUT_SECONDS
        if job_type in {"create_offer", "discover"}
        else SCAN_TIMEOUT_SECONDS
    )
    attempts = 3 if job_type in {"discover", "inspect"} else 1
    async with asyncio.timeout(timeout_seconds):
        for attempt in range(attempts):
            try:
                return await _execute_job(
                    job,
                    merchant_session=merchant_session,
                    store_id=store_id,
                )
            except Exception as exc:
                if attempt + 1 >= attempts or not _is_rate_limited(exc):
                    raise
                delay = min(15.0, 2.5 * (2 ** attempt))
                await asyncio.sleep(delay + random.uniform(0.2, 0.8))
    raise RuntimeError("Product Test job ended without a result")


async def _process_job(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_session: KaspiMerchantSession,
    store_id: str,
) -> None:
    job_id = int(job["id"])
    _log(
        f"Задание #{job_id}: {job.get('job_type') or 'inspect'}",
        workspace_id=workspace_id,
    )
    try:
        result = await _run_job_with_retry(
            job,
            merchant_session=merchant_session,
            store_id=store_id,
        )
        payload = {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "status": "succeeded",
            "result": result,
        }
    except Exception as exc:
        payload = {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "status": "failed",
            "result": {},
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:4000],
        }
    completed = await _post_json_with_retry(
        f"{api_url}/api/product-test-agent/jobs/{job_id}/complete",
        token,
        payload,
        operation=f"Сохранение задания #{job_id}",
    )
    _log(
        f"Задание #{job_id}: {(completed.get('job') or completed).get('status')}",
        workspace_id=workspace_id,
    )


def _confirm_and_clear_config(workspace_id: int, reason: str) -> bool:
    if os.name != "nt":
        return False
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        confirmed = messagebox.askyesno(
            "LEO Product Test Agent — настройки",
            (
                f"CRM отклонила настройки аккаунта {workspace_id}.\n\n{reason}\n\n"
                "Сбросить сохранённые данные Product Test Agent и пройти "
                "регистрацию заново? Настройки Fast Dumping Agent не изменятся."
            ),
            parent=root,
        )
        root.destroy()
    except Exception:
        return False
    if not confirmed:
        return False
    try:
        _config_path(workspace_id).unlink(missing_ok=True)
    except OSError:
        return False
    return True


async def main(
    *,
    once: bool = False,
    workspace_id: int | None = None,
    reconfigure: bool = False,
) -> int:
    selected_workspace = _workspace_id(workspace_id)
    _acquire_single_instance(selected_workspace)
    config = _load_config(selected_workspace)
    api_url = str(
        os.getenv("CRM_API_URL") or config.get("api_url") or DEFAULT_API_URL
    ).strip().rstrip("/")
    token = _secret_setting(
        config,
        key="service_token",
        env_name="CRM_SERVICE_TOKEN",
        prompt="SERVICE_API_TOKEN из Render",
        reconfigure=reconfigure,
    )
    merchant_uid = _plain_setting(
        config,
        key="merchant_uid",
        env_name="KASPI_MERCHANT_UID",
        prompt="Kaspi Merchant UID",
        reconfigure=reconfigure,
    )
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
    agent_id = str(
        os.getenv("PRODUCT_TEST_AGENT_ID")
        or config.get("agent_id")
        or f"product-test-{socket.gethostname()}-workspace-{selected_workspace}"
    ).strip()
    config.update(
        {
            "api_url": api_url,
            "workspace_id": selected_workspace,
            "agent_id": agent_id,
            "merchant_uid": merchant_uid,
            "store_id": store_id,
            "email": email,
        }
    )
    _save_config(config, selected_workspace)
    _ensure_ozon_session()

    merchant_session = KaspiMerchantSession(
        merchant_uid=merchant_uid,
        email=email,
        password=password,
        load_sid=lambda: _load_sid(config, selected_workspace),
        save_sid=lambda sid: _save_sid(config, selected_workspace, sid),
    )
    identity = _agent_payload(agent_id, selected_workspace, merchant_uid)
    try:
        await _post_json_with_retry(
            f"{api_url}/api/product-test-agent/heartbeat",
            token,
            {**identity, "status": "online"},
            operation="Подключение к CRM",
        )
    except CRMRequestError as exc:
        if (
            not reconfigure
            and any(marker in str(exc) for marker in ("HTTP 401", "HTTP 403", "HTTP 409", "HTTP 422"))
            and await asyncio.to_thread(
                _confirm_and_clear_config,
                selected_workspace,
                str(exc),
            )
        ):
            raise AgentReconfigureRequired(
                f"Настройки workspace {selected_workspace} сброшены; перезапустите Agent"
            ) from exc
        raise

    _log(
        f"LEO Product Test Agent {VERSION} · workspace {selected_workspace}",
        workspace_id=selected_workspace,
    )
    _log(
        "Выделенная очередь: Kaspi → Ozon → CRM → существующий Fast Dumping.",
        workspace_id=selected_workspace,
    )
    if os.name == "nt" and not once:
        _show_message(
            "LEO Product Test Agent",
            "Агент подключён. Поиск и добавление тестовых товаров теперь "
            "работают отдельно от мониторинга и Быстрого демпинга. Не закрывайте окно.",
        )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await _post_json_with_retry(
                    f"{api_url}/api/product-test-agent/heartbeat",
                    token,
                    {**identity, "status": "online"},
                    operation="Heartbeat",
                )
            except Exception as exc:
                _log(f"Heartbeat: {exc}", workspace_id=selected_workspace)

    async def worker() -> int:
        while True:
            try:
                claim = await _post_json_with_retry(
                    f"{api_url}/api/product-test-agent/claim",
                    token,
                    identity,
                    operation="Получение задания Теста товаров",
                )
                job = claim.get("job")
                if job:
                    await _process_job(
                        api_url=api_url,
                        token=token,
                        job=job,
                        agent_id=agent_id,
                        workspace_id=selected_workspace,
                        merchant_session=merchant_session,
                        store_id=store_id,
                    )
                    if once:
                        return 0
                    continue
                if once:
                    return 2
                await asyncio.sleep(
                    max(IDLE_POLL_SECONDS, float(claim.get("retry_after_seconds") or 1))
                )
            except AgentReconfigureRequired:
                raise
            except Exception as exc:
                _log(f"Worker: {exc}", workspace_id=selected_workspace)
                if once:
                    return 1
                await asyncio.sleep(3)

    if once:
        return await worker()
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        return await worker()
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LEO dedicated Product Test Agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--workspace-id", type=int)
    parser.add_argument("--reconfigure", action="store_true")
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
    except AgentReconfigureRequired as exc:
        _log(str(exc), workspace_id=args.workspace_id)
        _show_message(
            "LEO Product Test Agent — настройки сброшены",
            "Запустите Agent ещё раз и заново зарегистрируйте этот аккаунт.",
        )
        raise SystemExit(2)
    except Exception as exc:
        details = "".join(traceback.format_exception(exc)).strip()
        _log(details, workspace_id=args.workspace_id)
        _show_message(
            "LEO Product Test Agent — ошибка",
            f"{exc}\n\nПодробности: {_log_path(args.workspace_id)}",
            error=True,
        )
        if sys.stdin and sys.stdin.isatty():
            try:
                input("Нажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
        raise SystemExit(1)
