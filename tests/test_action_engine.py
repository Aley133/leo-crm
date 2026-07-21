from datetime import UTC, datetime
from decimal import Decimal

from backend.app.action_engine import ActionEngine
from backend.app.supplier_intelligence import BestOfferEngine, SupplierCandidate


NOW = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)


def candidate(
    binding_id: int,
    code: str,
    price: str | None,
    delivery_days: int | None,
    *,
    available: bool | None = True,
    primary: bool = False,
) -> SupplierCandidate:
    return SupplierCandidate(
        binding_id=binding_id,
        supplier_product_id=binding_id + 100,
        supplier_code=code,
        supplier_name=code.upper(),
        price=None if price is None else Decimal(price),
        currency="KZT",
        available=available,
        delivery_days=delivery_days,
        is_primary=primary,
        priority=0 if primary else 100,
        last_checked_at=NOW,
    )


def recommend(*items: SupplierCandidate):
    candidates = tuple(items)
    decision = BestOfferEngine.decide(candidates, now=NOW)
    return ActionEngine.recommend(candidates, decision)


def test_action_engine_keeps_confirmed_primary_winner() -> None:
    action = recommend(
        candidate(1, "ozon", "2632", 0, primary=True),
        candidate(2, "wb", "4530", 5),
    )

    assert action.kind == "no_action"
    assert action.severity == "success"
    assert action.target_supplier_code == "ozon"
    assert action.auto_apply_allowed is False


def test_action_engine_recommends_switch_without_mutating_state() -> None:
    action = recommend(
        candidate(1, "wb", "4530", 5, primary=True),
        candidate(2, "ozon", "2632", 0),
    )

    assert action.kind == "switch_supplier"
    assert action.severity == "warning"
    assert action.target_binding_id == 2
    assert action.target_supplier_code == "ozon"
    assert action.auto_apply_allowed is False


def test_action_engine_blocks_automation_when_only_one_offer_exists() -> None:
    action = recommend(candidate(1, "ozon", "2632", 0))

    assert action.kind == "collect_more_data"
    assert action.severity == "warning"
    assert "дополнительных наблюдений" in action.summary
    assert action.auto_apply_allowed is False


def test_action_engine_reports_no_available_offer() -> None:
    action = recommend(
        candidate(1, "ozon", None, None, available=False),
        candidate(2, "wb", None, None, available=False),
    )

    assert action.kind == "no_available_offer"
    assert action.severity == "critical"
    assert action.target_binding_id is None
    assert action.auto_apply_allowed is False


def test_action_engine_has_no_infrastructure_dependencies() -> None:
    source = open("backend/app/action_engine.py", encoding="utf-8").read()

    assert "sqlalchemy" not in source
    assert "FastAPI" not in source
    assert "Browser" not in source
    assert "XML" in source
    assert "auto_apply_allowed: bool = False" in source
