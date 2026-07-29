from __future__ import annotations

import json
from urllib.request import Request

import tools.browser_agent_desktop as desktop


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def test_cdp_ready_requires_browser_websocket(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop,
        "urlopen",
        lambda request, timeout: FakeResponse({"Browser": "Chrome/140"}),
    )

    assert desktop._cdp_ready() is False


def test_wait_for_profile_page_creates_page_when_cdp_has_no_targets(monkeypatch) -> None:
    targets: list[dict] = []
    created_requests: list[Request] = []

    def fake_urlopen(request, timeout):
        url = request.full_url if isinstance(request, Request) else str(request)
        if url.endswith("/json/version"):
            return FakeResponse(
                {
                    "Browser": "Chrome/140",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
                }
            )
        if url.endswith("/json/list"):
            return FakeResponse(targets)
        if "/json/new?" in url:
            assert isinstance(request, Request)
            assert request.get_method() == "PUT"
            created_requests.append(request)
            targets.append({"type": "page", "url": desktop.PROFILE_START_URL})
            return FakeResponse(targets[0])
        raise AssertionError(f"unexpected CDP request: {url}")

    monkeypatch.setattr(desktop, "urlopen", fake_urlopen)

    assert desktop._wait_for_profile_page(timeout_seconds=1) is True
    assert len(created_requests) == 1


def test_start_browser_reuses_cdp_only_after_profile_page_exists(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "_cdp_ready", lambda: True)
    monkeypatch.setattr(
        desktop,
        "_wait_for_profile_page",
        lambda *, timeout_seconds: timeout_seconds == 10,
    )
    monkeypatch.setattr(
        desktop,
        "_find_browser",
        lambda: (_ for _ in ()).throw(AssertionError("Chrome must not be launched twice")),
    )

    desktop._start_browser()
