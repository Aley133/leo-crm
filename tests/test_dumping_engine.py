from decimal import Decimal
from pathlib import Path

from backend.app.dumping_service import calculate_safe_floor, update_feed_xml


ROOT = Path(__file__).resolve().parents[1]


def test_safe_floor_preserves_requested_profit_across_logistics_band() -> None:
    assert calculate_safe_floor(
        unit_cost_kzt=Decimal("2300"),
        minimum_profit_kzt=Decimal("1000"),
    ) == Decimal("4179.00")


def test_safe_floor_uses_higher_logistics_band_when_required() -> None:
    assert calculate_safe_floor(
        unit_cost_kzt=Decimal("5640"),
        minimum_profit_kzt=Decimal("1000"),
    ) == Decimal("8956.00")


def test_feed_update_changes_only_matching_offer_price_and_preorder() -> None:
    source = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers>
      <offer sku='SKU-1'><cityprices><cityprice cityId='750000000'>9999</cityprice></cityprices><availability available='yes' preOrder='5'/></offer>
      <offer sku='SKU-2'><cityprices><cityprice cityId='750000000'>7777</cityprice></cityprices><availability available='yes' preOrder='2'/></offer>
    </offers></kaspi_catalog>"""

    generated = update_feed_xml(
        source,
        sku_candidates={"SKU-1"},
        price_kzt=Decimal("8956"),
        preorder_days=4,
    )

    assert "8956" in generated
    assert 'preOrder="4"' in generated
    assert "7777" in generated
    assert 'preOrder="2"' in generated


def test_feed_publication_locks_the_shared_xml_row() -> None:
    source = (ROOT / "backend" / "app" / "dumping_service.py").read_text(encoding="utf-8")

    assert "select(KaspiXmlFeed)" in source
    assert ".with_for_update()" in source


def test_dumping_workspace_is_exposed_in_crm_ui() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "dumping.html").read_text(encoding="utf-8")
    ingestion = (ROOT / "backend" / "app" / "browser_agent_ingestion.py").read_text(encoding="utf-8")

    assert '@router.get("/crm/dumping"' in ui
    assert "Минимальная прибыль" in html
    assert "Автопубликация после Supplier Snapshot" in html
    assert "mark_supplier_product_for_dumping_refresh" in ingestion
