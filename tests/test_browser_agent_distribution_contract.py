from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_agent_has_desktop_entrypoint_and_installer_workflow() -> None:
    entrypoint = (ROOT / "tools" / "browser_agent_desktop.py").read_text(encoding="utf-8")
    installer = (ROOT / "tools" / "windows" / "LEO-Browser-Agent.iss").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "browser-agent-release.yml").read_text(encoding="utf-8")

    assert (ROOT / "tools" / "__init__.py").is_file()
    assert "SERVICE_API_TOKEN" in entrypoint
    assert "CRM_API_URL" in entrypoint
    assert "CHROME_CDP_ENDPOINT" in entrypoint
    assert "run_browser_agent" in entrypoint
    assert "CryptProtectData" in entrypoint
    assert "CryptUnprotectData" in entrypoint
    assert 'APP_VERSION = "0.2.0"' in entrypoint
    assert "MUTEX_NAME" in entrypoint
    assert "_browser_watchdog" in entrypoint
    assert "CreateMutexW" in entrypoint
    assert "OutputBaseFilename=LEO-Browser-Agent-Setup" in installer
    assert "OutputDir=..\\..\\dist" in installer
    assert '#define MyAppVersion "0.2.0"' in installer
    assert "taskkill /F /IM" in installer
    assert "CloseApplications=yes" in installer
    assert "RestartApplications=no" in installer
    assert "ssPostInstall" in installer
    assert "LEO-Browser-Agent-Setup.exe" in workflow
    assert "pyinstaller" in workflow.lower()
    assert "--paths ." in workflow
    assert "--hidden-import tools.browser_agent" in workflow
    assert "python -c \"import tools.browser_agent; import tools.browser_agent_desktop\"" in workflow
    assert 'backend/app/supplier_adapters/**' in workflow
    assert "Verify release version alignment" in workflow
    assert "Inno Setup" in workflow
    assert "browser-agent-latest" in workflow
    assert "gh release upload" in workflow


def test_monitoring_page_downloads_windows_installer_directly() -> None:
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")

    assert 'id="download-browser-agent"' in html
    assert "releases/download/browser-agent-latest/LEO-Browser-Agent-Setup.exe" in html
    assert "LEO-Browser-Agent-Setup.cmd" not in html
    assert "Скачать Browser Agent" in html


def test_release_does_not_embed_service_token() -> None:
    entrypoint = (ROOT / "tools" / "browser_agent_desktop.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "browser-agent-release.yml").read_text(encoding="utf-8")
    installer = (ROOT / "tools" / "windows" / "LEO-Browser-Agent.iss").read_text(encoding="utf-8")

    assert "simpledialog.askstring" in entrypoint
    assert "PASTE_" not in entrypoint
    assert "SERVICE_API_TOKEN=" not in workflow
    assert "SERVICE_API_TOKEN=" not in installer


def test_agent_token_survives_installation_updates() -> None:
    entrypoint = (ROOT / "tools" / "browser_agent_desktop.py").read_text(encoding="utf-8")
    installer = (ROOT / "tools" / "windows" / "LEO-Browser-Agent.iss").read_text(encoding="utf-8")

    assert 'APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LEO-CRM" / "browser-agent"' in entrypoint
    assert 'TOKEN_FILE = APP_DIR / "agent-token.dat"' in entrypoint
    assert "DefaultDirName={localappdata}\\Programs\\LEO Browser Agent" in installer
    assert "agent-token.dat" not in installer
