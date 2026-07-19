from pathlib import Path

from tools.browser_agent import _parse_args


ROOT = Path(__file__).resolve().parents[1]


def test_windows_launcher_keeps_cdp_local_and_uses_dedicated_profile() -> None:
    script = (ROOT / "tools" / "windows" / "start_browser_agent.ps1").read_text(encoding="utf-8")
    assert "--remote-debugging-address=127.0.0.1" in script
    assert "--remote-debugging-port=9222" in script
    assert 'Join-Path $env:LOCALAPPDATA "LEO-CRM\\browser-agent"' in script
    assert '$ChromeProfile = Join-Path $RuntimeRoot "chrome-profile"' in script
    assert "0.0.0.0" not in script


def test_windows_agent_secret_file_and_profile_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tools/windows/browser_agent.env.ps1" in ignore
    assert ".browser-agent/" in ignore


def test_one_shot_launcher_passes_once_flag() -> None:
    wrapper = (ROOT / "tools" / "windows" / "verify_browser_agent_once.bat").read_text(encoding="utf-8")
    assert "-Once" in wrapper


def test_browser_agent_cli_accepts_once(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["browser_agent.py", "--once"])
    assert _parse_args().once is True
