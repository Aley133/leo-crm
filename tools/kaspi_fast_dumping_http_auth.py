from __future__ import annotations

import json
import re
from collections.abc import Callable
from threading import RLock
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from tools.kaspi_fast_dumping_session import (
    IDMC_LOGIN_PAGE,
    IDMC_LOGIN_URL,
    MC_OAUTH_ENTRY_URL,
    MC_ROOT_URL,
    KaspiMerchantSession,
)


CookieStateLoader = Callable[[], str | None]
CookieStateSaver = Callable[[str], None]
OtpPrompt = Callable[[str], str]
HttpClientFactory = Callable[..., httpx.Client]


def _is_kaspi_domain(value: str) -> bool:
    domain = value.lstrip(".").lower()
    return domain == "kaspi.kz" or domain.endswith(".kaspi.kz")


def _normalise_otp(value: str, *, digits: int = 6) -> str:
    code = re.sub(r"\D", "", value or "")
    if len(code) != digits:
        raise ValueError(f"Код Kaspi должен содержать {digits} цифр")
    return code


def _cookie_state(client: httpx.Client) -> str:
    cookies = []
    for cookie in client.cookies.jar:
        domain = str(cookie.domain or "")
        if domain and not _is_kaspi_domain(domain):
            continue
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": str(cookie.path or "/"),
            }
        )
    return json.dumps(
        {"version": 1, "cookies": cookies},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _restore_cookie_state(client: httpx.Client, raw: str | None) -> None:
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "").strip()
        path = str(item.get("path") or "/").strip() or "/"
        if not name or not _is_kaspi_domain(domain):
            continue
        client.cookies.set(name, value, domain=domain, path=path)


def _clear_cookie(client: httpx.Client, name: str) -> None:
    matches = [
        (cookie.domain, cookie.path, cookie.name)
        for cookie in client.cookies.jar
        if cookie.name == name
    ]
    for domain, path, cookie_name in matches:
        try:
            client.cookies.jar.clear(domain, path, cookie_name)
        except KeyError:
            pass


def _response_payload(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    if not response.is_success:
        error_code = None
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = body.get("errorCode") or body.get("code")
        except ValueError:
            pass
        suffix = f" ({error_code})" if error_code else ""
        raise RuntimeError(
            f"Kaspi отклонил {stage}: HTTP {response.status_code}{suffix}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Kaspi вернул не-JSON ответ на этапе: {stage}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Kaspi вернул неожиданный ответ на этапе: {stage}")
    return payload


def _safe_redirect_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    target = urljoin(IDMC_LOGIN_PAGE, value.strip())
    parsed = urlparse(target)
    if parsed.scheme != "https" or not _is_kaspi_domain(parsed.hostname or ""):
        raise RuntimeError("Kaspi вернул небезопасный redirectUrl")
    return target


class HttpOtpKaspiMerchantSession(KaspiMerchantSession):
    """Pure-HTTP Merchant session with interactive email MFA and trusted cookies."""

    def __init__(
        self,
        *,
        workspace_id: int,
        load_cookie_state: CookieStateLoader,
        save_cookie_state: CookieStateSaver,
        prompt_otp: OtpPrompt,
        log: Callable[[str], None] | None = None,
        client_factory: HttpClientFactory = httpx.Client,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.workspace_id = workspace_id
        self.load_cookie_state = load_cookie_state
        self.save_cookie_state = save_cookie_state
        self.prompt_otp = prompt_otp
        self.auth_log = log
        self.client_factory = client_factory
        self._http_client: httpx.Client | None = None
        self._auth_lock = RLock()

    def _emit(self, message: str) -> None:
        if self.auth_log is not None:
            self.auth_log(message)

    def _new_client(self) -> httpx.Client:
        client = self.client_factory(
            headers=self._headers(),
            follow_redirects=True,
            timeout=self.timeout_seconds,
        )
        _restore_cookie_state(client, self.load_cookie_state())
        return client

    def _reset_client(self) -> httpx.Client:
        if self._http_client is not None:
            self._http_client.close()
        self._http_client = self._new_client()
        return self._http_client

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = self._new_client()
        return self._http_client

    def _save_trusted_cookies(self, client: httpx.Client) -> None:
        self.save_cookie_state(_cookie_state(client))

    def _follow_redirect(self, client: httpx.Client, payload: dict[str, Any]) -> bool:
        target = _safe_redirect_url(payload.get("redirectUrl"))
        if target is None:
            return False
        response = client.get(target)
        if not self._cookie(client, "mc-sid"):
            response.raise_for_status()
        return True

    def _submit_credentials(self, client: httpx.Client) -> dict[str, Any]:
        response = client.post(
            IDMC_LOGIN_URL,
            json={"_u": self.email, "_p": self.password, "_r_d": True},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://idmc.shop.kaspi.kz",
                "Referer": IDMC_LOGIN_PAGE,
            },
            follow_redirects=False,
        )
        return _response_payload(response, stage="email/пароль")

    def _complete_login(self, client: httpx.Client, payload: dict[str, Any]) -> None:
        if payload.get("su"):
            selected = client.post(
                IDMC_LOGIN_URL,
                json={"_s_m": self.merchant_uid},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://idmc.shop.kaspi.kz",
                    "Referer": IDMC_LOGIN_PAGE,
                },
                follow_redirects=False,
            )
            payload = _response_payload(selected, stage="выбор продавца")

        if self._follow_redirect(client, payload):
            return

        recipient = str(payload.get("email") or "").strip()
        if not recipient:
            raise RuntimeError(
                "Kaspi не вернул ни redirectUrl, ни запрос кода из почты"
            )

        self._emit(
            "Kaspi запросил код из почты: включено «Запомнить вход с этого устройства»."
        )
        code = _normalise_otp(self.prompt_otp(recipient))
        response = client.post(
            IDMC_LOGIN_URL,
            json={"_m_c": code, "_r_d": True, "_u": recipient},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://idmc.shop.kaspi.kz",
                "Referer": IDMC_LOGIN_PAGE,
            },
            follow_redirects=False,
        )
        verified = _response_payload(response, stage="код из почты")
        if not self._follow_redirect(client, verified):
            raise RuntimeError("Kaspi принял код, но не вернул redirectUrl")

    def refresh_sid(self) -> str:
        with self._auth_lock:
            client = self._reset_client()
            _clear_cookie(client, "mc-sid")

            client.get(MC_ROOT_URL, follow_redirects=False)
            oauth = client.get(MC_OAUTH_ENTRY_URL)
            oauth.raise_for_status()
            sid = self._cookie(client, "mc-sid")
            if sid:
                self._save_trusted_cookies(client)
                self._emit("Доверенная HTTP-сессия Kaspi восстановлена без нового кода.")
                return sid

            login_page = client.get(IDMC_LOGIN_PAGE)
            login_page.raise_for_status()
            payload = self._submit_credentials(client)
            self._complete_login(client, payload)

            oauth = client.get(MC_OAUTH_ENTRY_URL)
            oauth.raise_for_status()
            sid = self._cookie(client, "mc-sid")
            if not sid:
                raise RuntimeError(
                    "Kaspi login/OAuth completed but mc-sid was not obtained"
                )
            self._save_trusted_cookies(client)
            self._emit("Новый mc-sid получен; Fast Dumping продолжает очередь автоматически.")
            return sid

    def authenticated_client(self, *, force_refresh: bool = False) -> httpx.Client:
        """Return the same trusted HTTP client used to obtain the current mc-sid."""

        with self._auth_lock:
            sid = None if force_refresh else self.load_sid()
            if not sid:
                sid, _ = self.ensure_valid_sid(force_refresh=force_refresh)
            client = self._client()
            _clear_cookie(client, "mc-sid")
            client.cookies.set(
                "mc-sid",
                sid,
                domain="mc.shop.kaspi.kz",
                path="/",
            )
            return client

    def close(self) -> None:
        with self._auth_lock:
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None
