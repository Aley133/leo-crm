from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tools import kaspi_fast_dumping_http_auth as http_auth
from tools import kaspi_fast_offer_runtime as offer_runtime


def _client_factory(transport: httpx.BaseTransport):
    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    return factory


def test_http_mfa_posts_code_and_remember_device() -> None:
    submitted: list[dict] = []
    saved_states: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/p/login":
            payload = json.loads(request.content)
            submitted.append(payload)
            if "_p" in payload:
                return httpx.Response(
                    200,
                    json={"email": "bar***@gmail.com"},
                    headers={
                        "set-cookie": (
                            "idmc-flow=flow-1; Domain=idmc.shop.kaspi.kz; Path=/"
                        )
                    },
                )
            return httpx.Response(
                200,
                json={
                    "redirectUrl": (
                        "https://mc.shop.kaspi.kz/oauth2/callback?state=test"
                    )
                },
            )
        if request.url.path == "/oauth2/callback":
            return httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        "mc-sid=fresh-sid; Domain=mc.shop.kaspi.kz; Path=/; HttpOnly"
                    )
                },
            )
        return httpx.Response(200, text="ok")

    prompted: list[str] = []
    session = http_auth.HttpOtpKaspiMerchantSession(
        workspace_id=1,
        merchant_uid="merchant-1",
        email="owner@example.test",
        password="secret",
        load_sid=lambda: None,
        save_sid=lambda _sid: None,
        load_cookie_state=lambda: None,
        save_cookie_state=saved_states.append,
        prompt_otp=lambda recipient: prompted.append(recipient) or "123 456",
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )

    assert session.refresh_sid() == "fresh-sid"
    assert prompted == ["bar***@gmail.com"]
    assert submitted == [
        {"_u": "owner@example.test", "_p": "secret", "_r_d": True},
        {"_m_c": "123456", "_r_d": True, "_u": "bar***@gmail.com"},
    ]
    assert saved_states
    saved = json.loads(saved_states[-1])
    assert {cookie["name"] for cookie in saved["cookies"]} >= {
        "idmc-flow",
        "mc-sid",
    }


def test_authenticated_client_reuses_saved_sid_without_second_login() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    session = http_auth.HttpOtpKaspiMerchantSession(
        workspace_id=1,
        merchant_uid="merchant-1",
        email="owner@example.test",
        password="secret",
        load_sid=lambda: "saved-sid",
        save_sid=lambda _sid: None,
        load_cookie_state=lambda: None,
        save_cookie_state=lambda _state: None,
        prompt_otp=lambda _recipient: pytest.fail("OTP must not be requested"),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )

    client = session.authenticated_client()

    assert requests == []
    assert any(
        cookie.name == "mc-sid" and cookie.value == "saved-sid"
        for cookie in client.cookies.jar
    )


def test_trusted_http_cookie_restores_session_without_new_code() -> None:
    trusted_state = json.dumps(
        {
            "version": 1,
            "cookies": [
                {
                    "name": "trusted-device",
                    "value": "yes",
                    "domain": "idmc.shop.kaspi.kz",
                    "path": "/",
                }
            ],
        }
    )
    seen_trusted_cookie: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorization/1":
            return httpx.Response(
                302,
                headers={"location": "https://idmc.shop.kaspi.kz/trusted"},
            )
        if request.url.path == "/trusted":
            seen_trusted_cookie.append(
                "trusted-device=yes" in request.headers.get("cookie", "")
            )
            return httpx.Response(
                302,
                headers={"location": "https://mc.shop.kaspi.kz/callback"},
            )
        if request.url.path == "/callback":
            return httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        "mc-sid=trusted-sid; Domain=mc.shop.kaspi.kz; Path=/"
                    )
                },
            )
        return httpx.Response(200)

    session = http_auth.HttpOtpKaspiMerchantSession(
        workspace_id=1,
        merchant_uid="merchant-1",
        email="owner@example.test",
        password="secret",
        load_sid=lambda: None,
        save_sid=lambda _sid: None,
        load_cookie_state=lambda: trusted_state,
        save_cookie_state=lambda _state: None,
        prompt_otp=lambda _recipient: pytest.fail("OTP must not be requested"),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )

    assert session.refresh_sid() == "trusted-sid"
    assert seen_trusted_cookie == [True]


def test_offer_runtime_prefers_session_authenticated_client() -> None:
    sentinel = object()
    calls: list[bool] = []

    class Session:
        def authenticated_client(self, *, force_refresh: bool = False):
            calls.append(force_refresh)
            return sentinel

    session = Session()
    assert offer_runtime._client(session) is sentinel
    assert offer_runtime._client(session, force_refresh=True) is sentinel
    assert calls == [False, True]


def test_cookie_state_is_restricted_to_kaspi_domains() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    client = httpx.Client(transport=transport)
    client.cookies.set("trusted", "yes", domain="idmc.shop.kaspi.kz", path="/")
    client.cookies.set("foreign", "no", domain="example.test", path="/")

    raw = http_auth._cookie_state(client)
    restored = httpx.Client(transport=transport)
    http_auth._restore_cookie_state(restored, raw)

    assert any(cookie.name == "trusted" for cookie in restored.cookies.jar)
    assert all(cookie.name != "foreign" for cookie in restored.cookies.jar)


@pytest.mark.parametrize("value", ["", "12345", "abcdef", "1234567"])
def test_http_mfa_rejects_invalid_code(value: str) -> None:
    with pytest.raises(ValueError, match="6"):
        http_auth._normalise_otp(value)


def test_http_auth_rejects_non_kaspi_redirect() -> None:
    with pytest.raises(RuntimeError, match="небезопасный"):
        http_auth._safe_redirect_url("https://notkaspi.kz/steal")


def test_fast_agent_release_is_http_only() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (
        root / ".github/workflows/kaspi-fast-dumping-agent-release.yml"
    ).read_text(encoding="utf-8")
    agent = (root / "tools/kaspi_fast_dumping_agent.py").read_text(
        encoding="utf-8"
    )

    assert 'VERSION = "1.2.5"' in agent
    assert "HttpOtpKaspiMerchantSession" in agent
    assert "kaspi_http_cookies_dpapi" in agent
    assert "tools/kaspi_fast_dumping_http_auth.py" in workflow
    assert '"playwright>=1.48,<2"' not in workflow
    assert "python -m playwright install" not in workflow
    assert "--collect-all playwright" not in workflow
    assert not (root / "tools/kaspi_fast_dumping_browser_auth.py").exists()
