from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_detail_api_owns_best_offer_decision() -> None:
    source = (ROOT / "backend" / "app" / "product_detail_api.py").read_text(encoding="utf-8")

    assert "BestOfferEngine.decide(candidates)" in source
    assert "best_offer: SupplierScoreRead | None" in source
    assert "supplier_scores: list[SupplierScoreRead]" in source
    assert "price_score: Decimal" in source
    assert "delivery_score: Decimal" in source
    assert "reasons: list[str]" in source


def test_product_card_renders_server_decision_without_resorting_prices() -> None:
    script = (ROOT / "backend" / "app" / "static" / "product-detail.js").read_text(encoding="utf-8")

    assert "best_offer: bestOffer" in script
    assert "supplier_scores: supplierScores" in script
    assert "renderBestOffer(bestOffer, bindings)" in script
    assert "Почему выбран" in script
    assert "price_score" in script
    assert "delivery_score" in script
    assert "candidates.sort" not in script
    assert "Number(left.price) - Number(right.price)" not in script
