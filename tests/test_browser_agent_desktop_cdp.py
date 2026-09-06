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


def test_desktop_agent_force_replaces_blocked_http_session(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Resolver:
        def resolve(self, *, validate=False):
            calls.append(("resolve", validate))
            return object()

        def import_curl(self, value, *, validate=False):
            calls.append(("import", (value, validate)))

    class Root:
        def withdraw(self):
            return None

        def destroy(self):
            return None

    monkeypatch.setattr(desktop, "OzonSessionResolver", Resolver)
    monkeypatch.setattr(desktop, "Tk", Root)
    monkeypatch.setattr(
        desktop.simpledialog,
        "askstring",
        lambda *args, **kwargs: "curl https://www.ozon.kz/search/?text=Solgar",
    )

    desktop._ensure_ozon_session(force_replace=True)

    assert calls == [
        (
            "import",
            ("curl https://www.ozon.kz/search/?text=Solgar", True),
        )
    ]


def test_desktop_agent_reprompts_after_invalid_http_session(monkeypatch) -> None:
    imports: list[str] = []
    errors: list[str] = []
    answers = iter(("curl invalid", "curl https://www.ozon.kz/search/?text=Solgar"))

    class Resolver:
        def resolve(self, *, validate=False):
            raise RuntimeError("saved session rejected")

        def import_curl(self, value, *, validate=False):
            imports.append(value)
            if value == "curl invalid":
                raise ValueError("invalid cURL")

    class Root:
        def withdraw(self):
            return None

        def destroy(self):
            return None

    monkeypatch.setattr(desktop, "OzonSessionResolver", Resolver)
    monkeypatch.setattr(desktop, "Tk", Root)
    monkeypatch.setattr(
        desktop.simpledialog,
        "askstring",
        lambda *args, **kwargs: next(answers),
    )
    monkeypatch.setattr(
        desktop.messagebox,
        "showerror",
        lambda _title, message, **_kwargs: errors.append(message),
    )

    desktop._ensure_ozon_session()

    assert imports == [
        "curl invalid",
        "curl https://www.ozon.kz/search/?text=Solgar",
    ]
    assert len(errors) == 1
    assert "вставьте его ещё раз" in errors[0]


def test_desktop_agent_has_no_cdp_or_browser_watchdog() -> None:
    source = (ROOT / "tools/browser_agent_desktop.py").read_text(encoding="utf-8")
    assert "CHROME_CDP_ENDPOINT" not in source
    assert "remote-debugging-port" not in source
    assert "Playwright" not in source
    assert "_browser_watchdog" not in source
    assert "OzonSessionResolver" in source
