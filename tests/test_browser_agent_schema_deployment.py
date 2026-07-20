from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_render_build_repairs_and_verifies_browser_agent_schema() -> None:
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "alembic upgrade head && python -m tools.ensure_browser_agent_schema" in render


def test_schema_repair_is_idempotent_and_never_drops_queue_data() -> None:
    script = (ROOT / "tools" / "ensure_browser_agent_schema.py").read_text(encoding="utf-8")
    normalized = script.casefold()

    assert normalized.count("checkfirst=true") >= 4
    assert "add column if not exists" in normalized
    assert "drop_table(" not in normalized
    assert "drop_index(" not in normalized
    assert ".drop(" not in normalized
    assert "_REQUIRED_COLUMNS" in script


def test_browser_ingestion_dependencies_are_repaired_and_verified() -> None:
    script = (ROOT / "tools" / "ensure_browser_agent_schema.py").read_text(encoding="utf-8")

    for table_name in (
        "browser_agent_jobs",
        "supplier_offer_states",
        "supplier_offer_observations",
        "pricing_policies",
        "fx_rate_snapshots",
        "price_calculations",
    ):
        assert f'"{table_name}"' in script

    assert '"currency": "VARCHAR(3)"' in script
    assert "_repair_safe_nullable_columns()" in script
    assert "PricingPolicy.__table__.create" in script
    assert "FxRateSnapshot.__table__.create" in script
    assert "PriceCalculation.__table__.create" in script
    assert "_verify_required_columns()" in script
