from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_action_engine_api_is_registered() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "action_api.py").read_text(encoding="utf-8")

    assert "from .action_api import router as action_router" in main
    assert "app.include_router(action_router)" in main
    assert 'prefix="/api/actions"' in api
    assert '@router.get("/products/{product_id}"' in api
    assert "ActionEngine.recommend(candidates, decision)" in api


def test_product_card_renders_server_side_action_recommendation() -> None:
    html = (ROOT / "backend" / "app" / "static" / "product-detail.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "Рекомендация CRM" in html
    assert 'id="action-center"' in html
    assert "/api/actions/products/${productId}" in script
    assert "renderActionCenter(action)" in script
    assert "Автоматические действия отключены" in script
    assert "auto_apply_allowed" in script


def test_product_card_does_not_execute_action_engine_decisions() -> None:
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "switch_supplier" in script
    assert "fetch(`/api/actions/products/${productId}`" in script
    assert "method:\"POST\"" not in script.split("const renderActionCenter", 1)[1].split("const renderDecisionTimeline", 1)[0]
    assert "/xml" not in script.split("const renderActionCenter", 1)[1].split("const renderDecisionTimeline", 1)[0]
