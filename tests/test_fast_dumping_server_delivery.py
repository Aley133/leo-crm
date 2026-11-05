from decimal import Decimal

import pytest

from backend.app.fast_dumping_delivery_filter import select_delivery_market
from backend.app.fast_dumping_service import complete_scan, prepare_apply, queue_scan
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _claim, _market, _seed_fast_product


def market(gap=1, fast_days=2):
    data = _market(own="20000", competitor="10000")
    data.update(offers_complete=True, page_visible_price_kzt="10000")
    data["offers"][0]["delivery_days"] = 2
    data["offers"][1].update(delivery_days=2 + gap, delivery_gap_days=gap,
        price_gap_kzt="10000", decision_reason="Выбран старым фильтром")
    data["offers"].append({"merchant_id": "fast", "merchant_name": "Fast seller",
        "price_kzt": "21000", "delivery_days": fast_days, "used_for_dumping": False})
    return data


@pytest.mark.parametrize("gap", [1, 2, 3, 10])
def test_old_agent_slow_selection_is_replaced_before_scan_and_write(db_session, gap):
    _, _, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        policy.delivery_advantage_days = 7
        policy.delivery_price_premium_kzt = 0
        job, _ = queue_scan(db_session, policy=policy, workspace_id=1, reason="test")
        db_session.commit()
    leased = _claim(db_session, 1)
    with workspace_context(1):
        result = complete_scan(db_session, workspace_id=1, job_id=job.id,
            agent_id="fast-agent", lease_token=leased.lease_token,
            succeeded=True, market_payload=market(gap))
        db_session.commit()
        assert result["queued_apply"]
        assert state.target_price_kzt == Decimal("20990")
        assert state.competitor_price_kzt == Decimal("21000")
        assert state.competitor_name == "Fast seller"
        assert not state.offers_json[1]["used_for_dumping"]
        assert "позже нашей" in state.offers_json[1]["decision_reason"]
    leased = _claim(db_session, 1)
    with workspace_context(1):
        prepared = prepare_apply(db_session, workspace_id=1, job_id=job.id,
            agent_id="fast-agent", lease_token=leased.lease_token)
        assert prepared["ready"]
        assert Decimal(str(prepared["target_price_kzt"])) == Decimal("20990")


@pytest.mark.parametrize("gap", [0, -1, None])
def test_same_faster_and_unknown_delivery_remain_competitors(gap):
    data = market()
    data["offers"][1].update(delivery_days=None if gap is None else 2 + gap,
        delivery_gap_days=gap)
    result = select_delivery_market(data["offers"], own_price=Decimal("20000"), complete=True)
    assert result["selected"]["price_kzt"] == "10000"


def test_absolute_days_override_stale_agent_gap_and_price_exclusions():
    data = market()
    data["offers"][1]["delivery_gap_days"] = -1
    result = select_delivery_market(data["offers"], own_price=Decimal("20000"), complete=True)
    assert result["selected"]["merchant_id"] == "fast"


@pytest.mark.parametrize("own_days,rival_days", [(None, 3), (2, None)])
def test_missing_absolute_delivery_does_not_use_a_stale_gap(own_days, rival_days):
    data = market()
    data["offers"][0]["delivery_days"] = own_days
    data["offers"][1]["delivery_days"] = rival_days
    result = select_delivery_market(data["offers"], own_price=Decimal("20000"), complete=True)
    assert result["selected"]["price_kzt"] == "10000"


@pytest.mark.parametrize("complete", [False, True])
def test_all_slower_does_not_create_a_price_ceiling_or_raise(complete):
    from backend.app.fast_dumping_service import _decide_policy_price
    from types import SimpleNamespace
    data = market(fast_days=5)
    state = SimpleNamespace(automation_json={"offers_complete": complete})
    decision = _decide_policy_price(db=None, policy=SimpleNamespace(pricing_mode="manual"),
        state=state, source=None, own_price_kzt=Decimal("20000"),
        competitor_price_kzt=Decimal("10000"), safe_floor_kzt=Decimal("15000"),
        undercut_step_kzt=Decimal(1), allow_price_raise=True,
        max_undercut_gap_percent=Decimal(35), market_offers=data["offers"],
        delivery_advantage_days=3, delivery_price_premium_kzt=500)
    assert decision.target_price_kzt == Decimal("20000")
    assert not decision.write_allowed
    assert decision.status == "delivery_advantage"


def test_partial_snapshot_does_not_choose_unselected_higher_price():
    data = market()
    result = select_delivery_market(data["offers"], own_price=Decimal("20000"),
        complete=False, selected_price=Decimal("10000"))
    assert result["selected"] is None


def test_delivery_filter_keeps_owned_peer_and_buyer_context_protection():
    data = market()
    data["offers"][1]["is_owned_peer"] = True
    data["offers"][1]["is_owned_group"] = True
    data["offers"].append({"price_kzt": "100", "delivery_days": 0})
    result = select_delivery_market(data["offers"], own_price=Decimal("20000"),
        page_price=Decimal("10000"), complete=True)
    assert result["selected"]["merchant_id"] == "fast"
    assert data["offers"][1] in result["offers"]
    assert not result["diagnostics"][-1]["used_for_dumping"]
