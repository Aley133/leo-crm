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
from tools.ozon_http import OzonSessionHttpAdapter

SUPPLIER_JOB_TYPE = "supplier_product_observation"
RUNTIME_KIND = "ozon_http"
DEFAULT_JOB_TIMEOUT_SECONDS = 45.0
HEARTBEAT_SECONDS = 15.0
IDLE_POLL_MAX_SECONDS = 15.0
SESSION_REFRESH_REQUIRED = "session_refresh_required"
SESSION_REFRESH_REQUIRED_EXIT = 75
_SESSION_REFRESH_ERROR_CODES = frozenset(
    {
        "AdapterAuthRequiredError",
        "AdapterBlockedError",
    }
)


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _post_json(url: str, token: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
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
        raise ValueError("Wildberries monitoring is temporarily disabled")
    raise ValueError(f"Unsupported supplier URL host: {host or '-'}")


async def _run_job(job: dict, adapters: dict[str, Any]) -> dict:
    job_type = str(job.get("job_type") or SUPPLIER_JOB_TYPE).strip()
    if job_type != SUPPLIER_JOB_TYPE:
        raise ValueError(f"Unsupported supplier Browser Agent job_type: {job_type}")

    supplier_product_id = int(job["supplier_product_id"])
    url = str(job["url"])
    adapter_code = _adapter_code_for_url(url)
    adapter = adapters[adapter_code]
    offer = await adapter.fetch(
        AdapterRequest(
            supplier_product_id=supplier_product_id,
            url=url,
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


async def _complete_job(*, api_url: str, token: str, job: dict, adapters: dict[str, Any]) -> str:
    print(f"Claimed HTTP monitoring job #{job['id']}: {job.get('url') or '-'}")
    timeout_seconds = max(10.0, float(os.getenv("BROWSER_AGENT_JOB_TIMEOUT_SECONDS") or DEFAULT_JOB_TIMEOUT_SECONDS))
    try:
        result = await asyncio.wait_for(_run_job(job, adapters), timeout=timeout_seconds)
        completion = {"lease_token": job["lease_token"], "status": "succeeded", "payload": result}
    except TimeoutError:
        completion = {
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": "HttpAgentJobTimeout",
            "error_message": f"HTTP monitoring job exceeded {timeout_seconds:g} seconds",
        }
    except Exception as exc:
        error_code = exc.__class__.__name__
        completion = {
            "lease_token": job["lease_token"],
            "status": "failed",
            "error_code": error_code,
            "error_message": str(exc)[:4000],
        }

    response = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/jobs/{job['id']}/complete",
        token,
        completion,
    )
    print(f"Completed HTTP monitoring job #{job['id']}: {completion['status']}")
    if completion.get("error_code") in _SESSION_REFRESH_ERROR_CODES:
        print(
            "Ozon HTTP session refresh required: "
            f"{completion['error_code']}"
        )
        return SESSION_REFRESH_REQUIRED
    return str(response.get("status") or completion["status"])


def _agent_identity(agent_id: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "runtime_kind": RUNTIME_KIND,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "version": (os.getenv("BROWSER_AGENT_VERSION") or "dev").strip(),
    }


async def _heartbeat_loop(*, api_url: str, token: str, agent_id: str) -> None:
    payload = {**_agent_identity(agent_id), "status": "idle"}
    while True:
        try:
            await asyncio.to_thread(
                _post_json,
                f"{api_url}/api/browser-agent/heartbeat",
                token,
                payload,
            )
        except Exception as exc:
            print(f"Heartbeat error: {exc}")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def _claim_one(*, api_url: str, token: str, agent_id: str, lease_seconds: int = 180) -> dict:
    return await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/claim",
        token,
        {
            **_agent_identity(agent_id),
            "lease_seconds": lease_seconds,
        },
    )


async def _dispatch_source_once(*, api_url: str, token: str, dispatch_limit: int, supplier_code: str) -> dict:
    result = await asyncio.to_thread(
        _post_json,
        f"{api_url}/api/browser-agent/dispatch-due",
        token,
        {"limit": dispatch_limit, "supplier_code": supplier_code},
    )
    queued = int(result.get("queued_count") or 0)
    print(f"Dispatcher queued {queued} due {supplier_code} monitor targets")
    return result


async def _dispatch_once(*, api_url: str, token: str, dispatch_limit: int) -> tuple[int, float]:
    queued = 0
    retry_after = 0.0
    # WB data and targets stay intact, but new WB jobs are intentionally not
    # dispatched until its HTTP engine is ready.
    for supplier_code in ("ozon",):
        result = await _dispatch_source_once(
            api_url=api_url,
            token=token,
            dispatch_limit=dispatch_limit,
            supplier_code=supplier_code,
        )
        queued += int(result.get("queued_count") or 0)
        retry_after = max(
            retry_after,
            float(result.get("retry_after_seconds") or 0),
        )
    return queued, retry_after


async def _dispatch_loop(
    *,
    api_url: str,
    token: str,
    poll_seconds: float,
    dispatch_limit: int,
    session_refresh_required: asyncio.Event,
) -> None:
    while not session_refresh_required.is_set():
        retry_after = poll_seconds
        try:
            _queued, retry_after = await _dispatch_once(
                api_url=api_url,
                token=token,
                dispatch_limit=dispatch_limit,
            )
        except Exception as exc:
            print(f"Dispatcher error: {exc}")
        try:
            await asyncio.wait_for(
                session_refresh_required.wait(),
                timeout=max(poll_seconds, retry_after),
            )
        except TimeoutError:
            pass


async def _pause_or_timeout(
    session_refresh_required: asyncio.Event,
    timeout: float,
) -> bool:
    """Return promptly when another worker detects an invalid Ozon session."""
    try:
        await asyncio.wait_for(
            session_refresh_required.wait(),
            timeout=max(0.0, timeout),
        )
    except TimeoutError:
        return False
    return True


async def _worker_loop(
    *,
    worker_number: int,
    api_url: str,
    token: str,
    agent_id: str,
    adapters: dict[str, Any],
    poll_seconds: float,
    session_refresh_required: asyncio.Event,
) -> None:
    worker_id = f"{agent_id}-w{worker_number}"
    idle_seconds = poll_seconds
    while not session_refresh_required.is_set():
        try:
            claim = await _claim_one(
                api_url=api_url,
                token=token,
                agent_id=worker_id,
            )
        except Exception as exc:
            print(f"Worker {worker_number} claim error: {exc}")
            if await _pause_or_timeout(session_refresh_required, poll_seconds):
                return
            continue
        job = claim.get("job")
        if not job:
            idle_seconds = min(
                IDLE_POLL_MAX_SECONDS,
                max(
                    poll_seconds,
                    float(claim.get("retry_after_seconds") or idle_seconds),
                    idle_seconds * 2,
                ),
            )
            if await _pause_or_timeout(session_refresh_required, idle_seconds):
                return
            continue
        idle_seconds = poll_seconds
        try:
            completion_status = await _complete_job(
                api_url=api_url,
                token=token,
                job=job,
                adapters=adapters,
            )
            if completion_status == SESSION_REFRESH_REQUIRED:
                session_refresh_required.set()
                return
        except Exception as exc:
            print(f"Worker {worker_number} completion error for job #{job['id']}: {exc}")


async def _run_once(*, api_url: str, token: str, agent_id: str, adapters: dict[str, Any], dispatch_limit: int) -> int:
    await _dispatch_once(api_url=api_url, token=token, dispatch_limit=dispatch_limit)
    claim = await _claim_one(
        api_url=api_url,
        token=token,
        agent_id=f"{agent_id}-once",
    )
    job = claim.get("job")
    if not job:
        print("No queued supplier HTTP jobs.")
        return 2
    status = await _complete_job(api_url=api_url, token=token, job=job, adapters=adapters)
    return 0 if status == "succeeded" else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LEO CRM local supplier HTTP agent")
    parser.add_argument("--once", action="store_true", help="dispatch and process exactly one supplier job, then exit")
    return parser.parse_args()


async def main(*, once: bool = False) -> int:
    api_url = _required_env("CRM_API_URL").rstrip("/")
    token = _required_env("CRM_SERVICE_TOKEN")
    agent_id = (os.getenv("BROWSER_AGENT_ID") or "leo-local-http").strip()
    poll_seconds = max(1.0, float(os.getenv("BROWSER_AGENT_POLL_SECONDS") or "3"))
    concurrency = max(1, min(12, int(os.getenv("BROWSER_AGENT_CONCURRENCY") or "3")))
    dispatch_limit = max(1, min(1000, int(os.getenv("BROWSER_AGENT_DISPATCH_LIMIT") or "100")))

    adapters: dict[str, Any] = {
        "ozon": OzonSessionHttpAdapter(),
    }
    print(f"HTTP agent {agent_id} connected to CRM {api_url}")
    print(f"Parallel HTTP workers: {1 if once else concurrency}")
    print("Enabled adapters: ozon (HTTP session); wb is temporarily disabled")

    if once:
        return await _run_once(
            api_url=api_url,
            token=token,
            agent_id=agent_id,
            adapters=adapters,
            dispatch_limit=1,
        )

    session_refresh_required = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            api_url=api_url,
            token=token,
            agent_id=agent_id,
        ),
        name="browser-agent-heartbeat",
    )
    dispatcher_task = asyncio.create_task(
        _dispatch_loop(
            api_url=api_url,
            token=token,
            poll_seconds=poll_seconds,
            dispatch_limit=dispatch_limit,
            session_refresh_required=session_refresh_required,
        ),
        name="browser-agent-dispatcher",
    )
    worker_tasks = [
        asyncio.create_task(
            _worker_loop(
                worker_number=number,
                api_url=api_url,
                token=token,
                agent_id=agent_id,
                adapters=adapters,
                poll_seconds=poll_seconds,
                session_refresh_required=session_refresh_required,
            ),
            name=f"browser-agent-worker-{number}",
        )
        for number in range(1, concurrency + 1)
    ]
    tasks = [heartbeat_task, dispatcher_task, *worker_tasks]
    refresh_waiter = asyncio.create_task(
        session_refresh_required.wait(),
        name="ozon-session-refresh-waiter",
    )
    try:
        done, _pending = await asyncio.wait(
            [*tasks, refresh_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if session_refresh_required.is_set():
            heartbeat_task.cancel()
            dispatcher_task.cancel()
            await asyncio.gather(
                heartbeat_task,
                dispatcher_task,
                return_exceptions=True,
            )
            # Workers already holding a lease are allowed to report their
            # result before the renewal dialog opens. Idle workers wake on the
            # shared event, so no lease is abandoned for three minutes.
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            return SESSION_REFRESH_REQUIRED_EXIT
        for task in done:
            if task is not refresh_waiter:
                task.result()
    finally:
        for task in [*tasks, refresh_waiter]:
            task.cancel()
        await asyncio.gather(*tasks, refresh_waiter, return_exceptions=True)
    return 0


if __name__ == "__main__":
    started = time.time()
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(main(once=args.once)))
    except KeyboardInterrupt:
        print(f"Browser agent stopped after {int(time.time() - started)} seconds")
