from pathlib import Path


def test_render_installs_playwright_browser_inside_deployed_application() -> None:
    config = Path("render.yaml").read_text(encoding="utf-8")

    assert "PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium" in config
    assert "- key: PLAYWRIGHT_BROWSERS_PATH" in config
    assert 'value: "0"' in config
