from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_render_build_repairs_and_verifies_browser_agent_schema() -> None:
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "alembic upgrade head && python -m tools.ensure_browser_agent_schema" in render


def test_schema_repair_is_idempotent_and_never_drops_queue_data() -> None:
    script = (ROOT / "tools" / "ensure_browser_agent_schema.py").read_text(encoding="utf-8")
    assert "checkfirst=True" in script
    assert '"browser_agent_jobs" not in inspector.get_table_names()' in script
    assert "drop" not in script.casefold()
    assert "_REQUIRED_COLUMNS" in script
