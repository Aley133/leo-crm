from pathlib import Path

from backend.app.main import app


STATIC_DIR = Path(__file__).resolve().parents[1] / "backend" / "app" / "static"


def test_login_and_account_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/login" in paths
    assert "/crm/account" in paths


def test_auth_screen_uses_workspace_session_and_plain_login_password() -> None:
    html = (STATIC_DIR / "auth.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert "Логин" in html
    assert "Пароль" in html
    assert "email" not in html.lower()
    assert "sms" not in html.lower()
    assert 'const SESSION_KEY = "leo_workspace_session"' in script
    assert "/api/auth/register" not in script  # endpoint is composed from mode
    assert "`/api/auth/${mode}`" in script


def test_account_screen_connects_only_current_workspace_kaspi() -> None:
    html = (STATIC_DIR / "account.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "account.js").read_text(encoding="utf-8")
    assert "Kaspi Partner ID" in html
    assert "Kaspi API Token" in html
    assert 'fetch("/api/workspace/kaspi"' in script
    assert 'Authorization: `Bearer ${token || ""}`' in script
    assert "SERVICE_API_TOKEN" not in html
    assert "SERVICE_API_TOKEN" not in script
