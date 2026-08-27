from __future__ import annotations

import asyncio
import sys
import time
import traceback

from tools import kaspi_fast_dumping_agent as base
from tools import kaspi_fast_offer_runtime as offer_runtime


VERSION = "1.2.1"
_RUNTIME_SESSION = None
_RUNTIME_STORE_ID: str | None = None


_original_plain_setting = base._plain_setting
_original_session_class = base.KaspiMerchantSession
_original_post_json_with_retry = base._post_json_with_retry


def _tracked_plain_setting(config: dict, *, key: str, env_name: str, prompt: str, reconfigure: bool) -> str:
    global _RUNTIME_STORE_ID
    value = _original_plain_setting(
        config,
        key=key,
        env_name=env_name,
        prompt=prompt,
        reconfigure=reconfigure,
    )
    if key == "store_id":
        _RUNTIME_STORE_ID = value
    return value


class TrackedMerchantSession(_original_session_class):
    def __init__(self, *args, **kwargs):
        global _RUNTIME_SESSION
        super().__init__(*args, **kwargs)
        _RUNTIME_SESSION = self


async def _post_json_with_offer_marker(
    url: str,
    token: str,
    payload: dict,
    *,
    operation: str,
) -> dict:
    # Agent 1.1 explicitly marks accepted-but-not-yet-observed Merchant writes.
    # The backend uses this marker to switch from the legacy 5-60 minute price
    # verify cadence to the new 15-second single-flight Merchant BFF verify.
    if (
        url.endswith("/apply-complete")
        and payload.get("accepted")
        and not payload.get("verified")
        and not payload.get("error_code")
    ):
        payload = {
            **payload,
            "error_code": "offer_state_pending",
            "error_message": "Kaspi accepted realtime offer-state; Merchant BFF confirmation is pending.",
        }
    return await _original_post_json_with_retry(
        url,
        token,
        payload,
        operation=operation,
    )


async def _verify_proxy(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
) -> None:
    if _RUNTIME_SESSION is None or not _RUNTIME_STORE_ID:
        raise RuntimeError("Fast Agent realtime context is not initialized")
    await offer_runtime.process_verify(
        api_url=api_url,
        token=token,
        job=job,
        agent_id=agent_id,
        workspace_id=workspace_id,
        merchant_uid=merchant_uid,
        merchant_session=_RUNTIME_SESSION,
        store_id=_RUNTIME_STORE_ID,
    )


base.VERSION = VERSION
base._plain_setting = _tracked_plain_setting
base.KaspiMerchantSession = TrackedMerchantSession
base._post_json_with_retry = _post_json_with_offer_marker
base._process_apply = offer_runtime.process_apply
base._process_verify = _verify_proxy


if __name__ == "__main__":
    started = time.time()
    args = base._parse_args()
    try:
        raise SystemExit(
            asyncio.run(
                base.main(
                    once=args.once,
                    workspace_id=args.workspace_id,
                    reconfigure=args.reconfigure,
                )
            )
        )
    except KeyboardInterrupt:
        base._log(f"Агент остановлен через {int(time.time() - started)} сек.")
    except base.AgentReconfigureRequired as exc:
        base._log(str(exc), workspace_id=args.workspace_id)
        base._show_message(
            "LEO Fast Dumping Agent — настройки сброшены",
            "Запустите Agent ещё раз и заново зарегистрируйте этот аккаунт.",
        )
        raise SystemExit(2)
    except Exception as exc:
        details = "".join(traceback.format_exception(exc)).strip()
        base._log(details)
        base._show_message(
            "LEO Fast Dumping Agent — ошибка",
            f"{exc}\n\nПодробности: {base._log_path(args.workspace_id)}",
            error=True,
        )
        if sys.stdin and sys.stdin.isatty():
            try:
                input("Нажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
        raise SystemExit(1)
