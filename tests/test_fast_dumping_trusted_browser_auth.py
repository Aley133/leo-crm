from __future__ import annotations

from pathlib import Path

import pytest

from tools import kaspi_fast_dumping_browser_auth as browser_auth


class _EmptyLocator:
    @property
    def first(self):
        return self

    def count(self) -> int:
        return 0

    def nth(self, _index: int):
        raise IndexError


class _Checkbox:
    def __init__(self) -> None:
        self.checked = False

    def is_visible(self) -> bool:
        return True

    def is_checked(self) -> bool:
        return self.checked

    def check(self, *, force: bool = False) -> None:
        assert force is True
        self.checked = True

    def get_attribute(self, name: str):
        if name == "role":
            return None
        return None


class _OneLocator:
    def __init__(self, item) -> None:
        self.item = item

    @property
    def first(self):
        return self.item

    def count(self) -> int:
        return 1

    def nth(self, index: int):
        assert index == 0
        return self.item


class _RememberPage:
    def __init__(self, checkbox: _Checkbox) -> None:
        self.checkbox = checkbox

    def locator(self, selector: str):
        if 'name*="remember"' in selector:
            return _OneLocator(self.checkbox)
        return _EmptyLocator()


def test_manual_otp_normalization_accepts_formatting() -> None:
    assert browser_auth._normalise_manual_otp("123 456", 6) == "123456"
    assert browser_auth._normalise_manual_otp("123-456", 6) == "123456"


def test_manual_otp_normalization_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="6"):
        browser_auth._normalise_manual_otp("12345", 6)


def test_browser_profile_is_durable_and_isolated_by_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KASPI_FAST_DUMPING_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))

    first = browser_auth.browser_profile_dir(1)
    second = browser_auth.browser_profile_dir(3)

    assert first == (tmp_path / "LEO CRM/kaspi_trusted_browser/workspace-1").resolve()
    assert second == (tmp_path / "LEO CRM/kaspi_trusted_browser/workspace-3").resolve()
    assert first != second


def test_browser_profile_override_supports_workspace_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "KASPI_FAST_DUMPING_BROWSER_PROFILE_DIR",
        str(tmp_path / "trusted-{workspace_id}"),
    )
    assert browser_auth.browser_profile_dir(7) == (tmp_path / "trusted-7").resolve()


def test_remember_user_checks_named_checkbox() -> None:
    checkbox = _Checkbox()
    assert browser_auth._remember_user(_RememberPage(checkbox)) is True
    assert checkbox.checked is True


def test_sensitive_oauth_query_values_are_redacted() -> None:
    redacted = browser_auth._redact_url(
        "https://idmc.shop.kaspi.kz/callback?code=secret&state=opaque&safe=yes"
    )
    assert "secret" not in redacted
    assert "opaque" not in redacted
    assert "code=%2A%2A%2A" in redacted
    assert "safe=yes" in redacted


def test_trusted_session_uses_browser_recovery_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def refresh(**kwargs):
        captured.update(kwargs)
        return "fresh-mc-sid"

    monkeypatch.delenv("KASPI_FAST_DUMPING_AUTH_MODE", raising=False)
    monkeypatch.setattr(browser_auth, "refresh_mc_sid_via_browser", refresh)
    session = browser_auth.TrustedBrowserKaspiMerchantSession(
        workspace_id=3,
        merchant_uid="merchant",
        email="owner@example.test",
        password="password",
        load_sid=lambda: None,
        save_sid=lambda _sid: None,
    )

    assert session.refresh_sid() == "fresh-mc-sid"
    assert captured["workspace_id"] == 3
    assert captured["email"] == "owner@example.test"
    assert captured["password"] == "password"
    assert captured["timeout"] >= 60


def test_legacy_http_recovery_remains_an_emergency_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KASPI_FAST_DUMPING_AUTH_MODE", "legacy_http")
    monkeypatch.setattr(
        browser_auth.KaspiMerchantSession,
        "refresh_sid",
        lambda _self: "legacy-sid",
    )
    session = browser_auth.TrustedBrowserKaspiMerchantSession(
        workspace_id=1,
        merchant_uid="merchant",
        email="owner@example.test",
        password="password",
        load_sid=lambda: None,
        save_sid=lambda _sid: None,
    )
    assert session.refresh_sid() == "legacy-sid"


def test_fast_agent_release_bundles_trusted_browser_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (
        root / ".github/workflows/kaspi-fast-dumping-agent-release.yml"
    ).read_text(encoding="utf-8")
    agent = (root / "tools/kaspi_fast_dumping_agent.py").read_text(
        encoding="utf-8"
    )
    auth = (root / "tools/kaspi_fast_dumping_browser_auth.py").read_text(
        encoding="utf-8"
    )

    assert 'VERSION = "1.2.4"' in agent
    assert "TrustedBrowserKaspiMerchantSession" in agent
    assert "workspace_id=selected_workspace" in agent
    assert "tools/kaspi_fast_dumping_browser_auth.py" in workflow
    assert '"playwright>=1.48,<2"' in workflow
    assert '$env:PLAYWRIGHT_BROWSERS_PATH="0"' in workflow
    assert "python -m playwright install chromium" in workflow
    assert "--collect-all playwright" in workflow
    assert 'context.clear_cookies(name="mc-sid")' in auth
    assert "context.clear_cookies()" not in auth
    assert "rmtree" not in auth
