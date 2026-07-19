from pathlib import Path


BROWSER_PATH = "/opt/render/project/src/.cache/playwright"


def test_render_uses_one_persistent_playwright_path_for_build_and_runtime() -> None:
    config = Path("render.yaml").read_text(encoding="utf-8")

    assert (
        f"PLAYWRIGHT_BROWSERS_PATH={BROWSER_PATH} "
        "python -m playwright install chromium"
    ) in config
    assert (
        f"startCommand: PLAYWRIGHT_BROWSERS_PATH={BROWSER_PATH} "
        "uvicorn backend.app.main:app"
    ) in config
    assert "- key: PLAYWRIGHT_BROWSERS_PATH" in config
    assert f"value: {BROWSER_PATH}" in config
