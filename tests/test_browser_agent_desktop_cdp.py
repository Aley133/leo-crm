from __future__ import annotations

from pathlib import Path

import tools.browser_agent_desktop as desktop


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_agent_reuses_valid_http_session_without_prompt(monkeypatch) -> None:
    calls: list[bool] = []

    class Resolver:
        def resolve(self, *, validate=False):
            calls.append(validate)
            return object()

    monkeypatch.setattr(desktop, "OzonSessionResolver", Resolver)
    desktop._ensure_ozon_session()
    assert calls == [True]


def test_desktop_agent_has_no_cdp_or_browser_watchdog() -> None:
    source = (ROOT / "tools/browser_agent_desktop.py").read_text(encoding="utf-8")
    assert "CHROME_CDP_ENDPOINT" not in source
    assert "remote-debugging-port" not in source
    assert "Playwright" not in source
    assert "_browser_watchdog" not in source
    assert "OzonSessionResolver" in source
