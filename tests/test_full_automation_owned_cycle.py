from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from backend.app.full_automation_pricing import decide_automated_price
from backend.app.full_automation_owned_cycle import read_shared_cycle
from backend.app.fast_dumping_models import FastDumpingPolicy, FastDumpingState
from backend.app.models import Product
from backend.app.workspace_models import Workspace
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _seed_fast_product


def market(shop, prices=(14499, 14500), external=(13999, 14000, 18000), delivery=(1, 1)):
    return [
        {'merchant_id': 'bar' if i == 0 else 'leo', 'is_own': i == shop,
         'is_owned_group': True, 'price_kzt': price, 'delivery_days': delivery[i]}
        for i, price in enumerate(prices)
    ] + [{'merchant_id': f'other{i}', 'price_kzt': price, 'delivery_days': 6 if i < 2 else 1}
         for i, price in enumerate(external)]


def choose(shop, cycle, prices=(14499, 14500), **kwargs):
    return decide_automated_price(own_price_kzt=prices[shop], safe_floor_kzt=kwargs.pop('floor', 13000),
                                  market_offers=kwargs.pop('rows', market(shop, prices)), unit_cost_kzt=9000,
                                  owned_cycle=cycle, offers_complete=True, **kwargs)


def test_user_example_raises_both_prices_without_consuming_peer_rank_slot():
    cycle = {}
    assert choose(0, cycle).target_price_kzt == 14599
    assert choose(1, cycle, (14599, 14500)).target_price_kzt == 14600
    assert cycle['base_kzt'] == '14499.00'
    assert cycle['phase'] == 'raise'
    # Do not lower again while the slower second agent is still at its original price.
    assert choose(0, cycle, (14599, 14500)).target_price_kzt == 14599


def test_slower_external_sellers_do_not_restart_peer_price_race():
    cycle = {}
    prices = [14499, 14500]
    for _ in range(8):
        for shop in (0, 1):
            before = prices[shop]
            decision = choose(shop, cycle, prices)
            prices[shop] = decision.target_price_kzt
            assert prices[shop] >= before
    # The 16000 logistics tariff makes 15999 more profitable than 16098.
    assert prices == [15999, 15999]
    for shop in (0, 1):
        assert not choose(shop, cycle, prices).write_allowed


@pytest.mark.parametrize("shop", [0, 1])
@pytest.mark.parametrize("own_days,rival_days,config,position,expected", [
    (1, 2, {}, 3, 6508),       # One day faster: do not follow the owned peer down.
    (2, 2, {}, 3, 6198),       # Equal delivery still requires price competition.
    (3, 2, {}, 3, 6198),       # Faster competitor.
    (None, 2, {}, 3, 6198),    # No unconfirmed delivery advantage.
    (1, None, {}, 3, 6198),
    (1, 2, {"delivery_premium_percent_per_day": 0}, 3, 6198),
    (1, 2, {"maximum_price_kzt": 6250}, 3, 6250),
    (1, 2, {"maximum_price_kzt": 6200}, 3, 6200),
    (1, 2, {}, 1, 6198),       # Explicit TOP-1 leaves no cheaper rival ahead.
])
def test_shared_step_uses_delivery_adjusted_market_limit(shop, own_days, rival_days, config, position, expected):
    prices = (6200, 6199) if shop == 0 else (6199, 6200)
    rows = market(shop, prices, external=(6199, 6500, 7000), delivery=(own_days, own_days))
    for row in rows[2:]:
        row['delivery_days'] = rival_days
    cycle = {"version": 1, "participants": ["bar", "leo"], "base_kzt": "6099",
             "phase": "step", "phase_changed_at": datetime.now(UTC).isoformat()}
    decision = choose(shop, cycle, prices, floor=4000, rows=rows, config=config, target_position=position)
    assert decision.target_price_kzt == expected
    assert decision.write_allowed == (expected != 6200)


def test_one_day_advantage_cannot_override_another_equally_fast_rival():
    rows = market(0, (6200, 6199), external=(6199, 6200, 7000))
    rows[2]['delivery_days'] = 2
    rows[3]['delivery_days'] = 1
    cycle = {"version": 1, "participants": ["bar", "leo"], "base_kzt": "6099",
             "phase": "step", "phase_changed_at": datetime.now(UTC).isoformat()}
    decision = choose(0, cycle, (6200, 6199), floor=4000, rows=rows)
    assert decision.target_price_kzt <= 6199


def test_no_external_pressure_raises_to_market_instead_of_freezing_old_cycle():
    cycle = {}
    rows = market(0, external=(17000, 17500, 18000))
    choose(0, cycle, rows=rows)
    assert choose(0, cycle, (14599, 14600), rows=market(0, (14599,14600), external=(17000,17500,18000))).target_price_kzt == 17999


def test_external_delivery_price_cap_and_floor_remain_authoritative():
    cycle = {}
    result = choose(0, cycle, rows=market(0, external=(14450, 15000, 18000), delivery=(6,6)))
    assert result.target_price_kzt == 14449
    assert choose(0, {}, config={'maximum_price_kzt':14520}).target_price_kzt == 14520
    result = choose(0, {}, floor=16000)
    assert result.target_price_kzt >= 16000
    assert result.status == 'floor_limited'


def test_phase_changed_by_peer_rejects_older_scan():
    scanned = datetime.now(UTC)
    cycle = {}
    choose(0, cycle, observed_at=scanned)
    choose(1, cycle, (14599,14600), observed_at=scanned+timedelta(seconds=5))
    result = choose(0, cycle, (14599,14600), observed_at=scanned)
    assert result.status == 'owned_cycle_sync' and not result.write_allowed


def test_logistics_jump_does_not_make_raise_unprofitable():
    cycle = {}
    result = choose(0, cycle, (4999,5000), floor=4000, rows=market(0, (4999,5000), external=(6000,7000,8000)))
    from backend.app.commerce.profit_calculator import kaspi_logistics_per_unit, KASPI_COMMISSION_RATE, TAX_RATE
    def margin(price):
        return Decimal(price) * (1 - KASPI_COMMISSION_RATE - TAX_RATE) - kaspi_logistics_per_unit(price)
    assert margin(result.target_price_kzt) >= margin(4999)
    assert result.target_price_kzt <= 6900


def test_shared_phase_is_read_across_workspaces_only_for_same_card_and_context(db_session):
    product, _, policy, state = _seed_fast_product(db_session)
    policy.pricing_mode = 'automation'
    state.offers_json = market(0)
    db_session.add(Workspace(id=3, name='LeoXpress', slug='leoxpress', is_active=True))
    db_session.flush()
    with workspace_context(3):
        peer = Product(name='Peer', kaspi_product_id=product.kaspi_product_id, merchant_sku='peer')
        db_session.add(peer); db_session.flush()
        peer_policy = FastDumpingPolicy(product_id=peer.id, pricing_mode='automation', city_id=policy.city_id, zone_id=policy.zone_id)
        db_session.add(peer_policy); db_session.flush()
        cycle = {}; choose(1, cycle)
        peer_state = FastDumpingState(policy_id=peer_policy.id, product_id=peer.id, automation_json={'owned_cycle':cycle})
        db_session.add(peer_state); db_session.commit()
    with workspace_context(1):
        assert read_shared_cycle(db_session, policy=policy, state=state) == cycle
    with workspace_context(3):
        peer_policy.city_id = 'different-city'; db_session.commit()
    with workspace_context(1):
        assert read_shared_cycle(db_session, policy=policy, state=state) == {}


def test_large_existing_peer_gap_does_not_lower_high_margin_shop_to_peer():
    cycle = {}
    result = choose(0, cycle, (15000,13000))
    assert result.target_price_kzt > 15000
    assert choose(1, cycle, (15000,13000)).target_price_kzt > 15000


def test_genuine_market_raise_keeps_new_margin_and_rebases_cycle():
    cycle = {}
    choose(0, cycle)
    result = choose(0, cycle, (15000,15001))
    assert result.target_price_kzt == 15100
    assert Decimal(cycle['base_kzt']) == 15000


def test_barwork_omega_catches_up_to_external_market_with_high_peer():
    for cycle in ({}, {"version": 1, "participants": ["bar", "leo"], "base_kzt": "6077",
                       "phase": "step", "phase_changed_at": datetime.now(UTC).isoformat()}):
        rows = market(0, (6177, 7952), external=(7297, 7297, 7297), delivery=(2, 2))
        for row in rows[2:]:
            row["delivery_days"] = 2
        decision = decide_automated_price(own_price_kzt=6177, safe_floor_kzt=5940,
            unit_cost_kzt=3422, market_offers=rows, offers_complete=True, owned_cycle=cycle)
        assert decision.target_price_kzt == 7296
        assert decision.write_allowed


def test_market_raise_still_avoids_unprofitable_tariff_crossing():
    rows = market(0, (4999, 5000), external=(5001, 5002, 5003), delivery=(2, 2))
    for row in rows[2:]:
        row["delivery_days"] = 2
    decision = decide_automated_price(own_price_kzt=4999, safe_floor_kzt=4000,
        unit_cost_kzt=3000, market_offers=rows, offers_complete=True, owned_cycle={})
    assert decision.target_price_kzt == 4999
    assert not decision.write_allowed
