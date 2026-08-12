from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36"
)
MC_ROOT_URL = "https://mc.shop.kaspi.kz/"
MC_OAUTH_ENTRY_URL = (
    "https://mc.shop.kaspi.kz/oauth2/authorization/1?"
    "redirectUrl=https%3A%2F%2Fkaspi.kz%2Fmc%2F"
)
IDMC_LOGIN_PAGE = "https://idmc.shop.kaspi.kz/login"
IDMC_LOGIN_URL = "https://idmc.shop.kaspi.kz/api/p/login"
PROCESS_URL = "https://mc.shop.kaspi.kz/pricefeed/upload/merchant/process"


class KaspiMerchantSession:
    """Pure-HTTP Merchant Cabinet session used only by the local Windows agent."""

    def __init__(
        self,
        *,
        merchant_uid: str,
        email: str,
        password: str,
        load_sid: Callable[[], str | None],
        save_sid: Callable[[str], None],
        timeout_seconds: float = 20.0,
    ) -> None:
        self.merchant_uid = merchant_uid.strip()
        self.email = email.strip()
        self.password = password
        self.load_sid = load_sid
        self.save_sid = save_sid
        self.timeout_seconds = timeout_seconds
        self.user_agent = DEFAULT_USER_AGENT

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": "ru,en-US;q=0.9,en;q=0.8,kk;q=0.7",
        }

    @staticmethod
    def _cookie(client: httpx.Client, name: str) -> str | None:
        matches = [cookie.value for cookie in client.cookies.jar if cookie.name == name]
        return matches[-1] if matches else None

    def probe(self, mc_sid: str) -> dict[str, Any]:
        url = (
            "https://mc.shop.kaspi.kz/pricefeed/upload/merchant/upload/"
            f"configuration?merchantUid={self.merchant_uid}"
        )
        started = time.perf_counter()
        try:
            response = httpx.post(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Cookie": f"mc-sid={mc_sid}",
                    "X-Auth-Version": "3",
                    "Origin": "https://kaspi.kz",
                    "Referer": "https://kaspi.kz/",
                    "User-Agent": self.user_agent,
                },
                timeout=self.timeout_seconds,
            )
            return {
                "ok": response.status_code == 200
                and response.headers.get("X-Principal-Type") == "2",
                "status_code": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def refresh_sid(self) -> str:
        with httpx.Client(
            headers=self._headers(),
            follow_redirects=True,
            timeout=self.timeout_seconds,
        ) as client:
            client.get(MC_ROOT_URL, follow_redirects=False)
            client.get(MC_OAUTH_ENTRY_URL, follow_redirects=False)
            client.get(IDMC_LOGIN_PAGE)
            login = client.post(
                IDMC_LOGIN_URL,
                json={"_u": self.email, "_p": self.password, "_r_d": False},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://idmc.shop.kaspi.kz",
                    "Referer": "https://idmc.shop.kaspi.kz/login",
                },
                follow_redirects=False,
            )
            login.raise_for_status()
            client.get(MC_OAUTH_ENTRY_URL).raise_for_status()
            sid = self._cookie(client, "mc-sid")
            if not sid:
                raise RuntimeError(
                    "Kaspi login/OAuth completed but mc-sid was not obtained"
                )
            return sid

    def ensure_valid_sid(self, *, force_refresh: bool = False) -> tuple[str, bool]:
        sid = None if force_refresh else self.load_sid()
        if sid:
            probe = self.probe(sid)
            if probe.get("ok"):
                return sid, False
            if probe.get("status_code") not in (401, 403):
                raise RuntimeError(f"Kaspi session probe failed: {probe}")
        sid = self.refresh_sid()
        probe = self.probe(sid)
        if not probe.get("ok"):
            raise RuntimeError(f"Fresh mc-sid rejected by Kaspi: {probe}")
        self.save_sid(sid)
        return sid, True

    def write_price(
        self,
        *,
        mc_sid: str,
        store_id: str,
        city_id: str,
        sku: str,
        model: str,
        stock_count: int,
        price: int,
    ) -> dict[str, Any]:
        payload = {
            "merchantUid": self.merchant_uid,
            "availabilities": [
                {
                    "available": "yes",
                    "storeId": store_id,
                    "stockCount": int(stock_count),
                }
            ],
            "cityPrices": [{"cityId": city_id, "value": int(price)}],
            "sku": sku,
            "model": model,
        }
        started = time.perf_counter()
        try:
            response = httpx.post(
                PROCESS_URL,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Cookie": f"mc-sid={mc_sid}",
                    "X-Auth-Version": "3",
                    "Origin": "https://kaspi.kz",
                    "Referer": "https://kaspi.kz/",
                    "User-Agent": self.user_agent,
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            try:
                body = response.json()
            except ValueError:
                body = None
            return {
                "accepted": response.is_success,
                "status_code": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "operation_id": body.get("id") if isinstance(body, dict) else None,
                "error_message": (
                    None
                    if response.is_success
                    else f"Kaspi Merchant returned HTTP {response.status_code}"
                ),
            }
        except httpx.HTTPError as exc:
            return {
                "accepted": False,
                "status_code": None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "operation_id": None,
                "error_message": f"{type(exc).__name__}: {exc}",
            }
