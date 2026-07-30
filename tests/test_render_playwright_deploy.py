from pathlib import Path


def test_render_web_service_does_not_install_or_start_server_side_browser() -> None:
    config = Path("render.yaml").read_text(encoding="utf-8")

    assert "python -m playwright install chromium" not in config
    assert "PLAYWRIGHT_BROWSERS_PATH" not in config
    assert "startCommand: uvicorn backend.app.main:app" in config
    assert "alembic upgrade head" in config
