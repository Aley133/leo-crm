from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_browser_agent_launcher_starts_cdp_and_worker() -> None:
    batch = (ROOT / "START_BROWSER_AGENT.bat").read_text(encoding="utf-8")
    script = (ROOT / "tools" / "start_browser_agent_windows.ps1").read_text(encoding="utf-8")

    assert "start_browser_agent_windows.ps1" in batch
    assert "--remote-debugging-port=9222" in script
    assert "CHROME_CDP_ENDPOINT" in script
    assert "CRM_API_URL" in script
    assert "CRM_SERVICE_TOKEN" in script
    assert "BROWSER_AGENT_ID" in script
    assert "python.exe" in script
    assert "-m tools.browser_agent" in script
    assert "Read-Host" in script
    assert "SecureString" in script


def test_monitoring_page_explains_persistent_connection() -> None:
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")
    connection_script = (ROOT / "backend" / "app" / "static" / "connection-status.js").read_text(encoding="utf-8")

    assert 'id="crm-connection"' in html
    assert 'id="disconnect"' in html
    assert 'auth-panel hidden' in html
    assert '/static/connection-status.js' in html
    assert 'localStorage.getItem(storageKey)' in connection_script
    assert 'localStorage.removeItem(storageKey)' in connection_script
    assert 'CRM подключена' in connection_script
    assert 'CRM не подключена' in connection_script
