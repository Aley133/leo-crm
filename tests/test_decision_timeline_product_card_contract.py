from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_detail_api_builds_decision_timeline_projection() -> None:
    source = (ROOT / "backend" / "app" / "product_detail_api.py").read_text(encoding="utf-8")

    assert "DecisionTimelineProjector.project(" in source
    assert "decision_timeline: list[DecisionTimelineEntryRead]" in source
    assert "TimelineObservation(" in source
    assert "TimelineBinding(" in source


def test_product_card_renders_server_decision_timeline() -> None:
    html = (ROOT / "backend" / "app" / "static" / "product-detail.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "История решений" in html
    assert 'id="decision-timeline"' in html
    assert "decision_timeline: decisionTimeline" in script
    assert "renderDecisionTimeline(decisionTimeline)" in script
    assert "Смена лидера" in script
    assert "DecisionTimelineProjector" not in script
