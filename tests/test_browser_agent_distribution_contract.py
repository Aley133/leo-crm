from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_agent_has_desktop_entrypoint_and_release_workflow() -> None:
    entrypoint = (ROOT / "tools" / "browser_agent_desktop.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "browser-agent-release.yml").read_text(encoding="utf-8")

    assert "SERVICE_API_TOKEN" in entrypoint
    assert "CRM_API_URL" in entrypoint
    assert "CHROME_CDP_ENDPOINT" in entrypoint
    assert "run_browser_agent" in entrypoint
    assert "LEO-Browser-Agent.exe" in workflow
    assert "pyinstaller" in workflow.lower()
    assert "softprops/action-gh-release" in workflow


def test_monitoring_page_exposes_stable_agent_download() -> None:
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")

    assert 'id="download-browser-agent"' in html
    assert "releases/latest/download/LEO-Browser-Agent.exe" in html
    assert "Скачать Browser Agent" in html


def test_release_does_not_embed_service_token() -> None:
    entrypoint = (ROOT / "tools" / "browser_agent_desktop.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "browser-agent-release.yml").read_text(encoding="utf-8")

    assert "simpledialog.askstring" in entrypoint
    assert "PASTE_" not in entrypoint
    assert "SERVICE_API_TOKEN=" not in workflow
