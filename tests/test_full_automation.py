from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from fastapi import HTTPException
from backend.app.full_automation_pricing import decide_automated_price
from backend.app.full_automation_api import (
    save_automation,
    AutomationSettings,
    read_automation,
)
from backend.app.fast_dumping_api import (
    upsert_fast_dumping_policy,
    FastDumpingPolicyUpsert,
)
from backend.app.fast_dumping_models import FastDumpingPolicy, FastDumpingJob
from backend.app import (
    fast_dumping_service as svc,
    fast_dumping_offer_runtime as runtime,
)
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _seed_fast_product
from backend.app.inventory_models import InventoryBatch


def offers():
    return [
        {"is_own": True, "price_kzt": "14000", "delivery_days": 1},
        {"price_kzt": "13999", "delivery_days": 5},
        {"price_kzt": "13999", "delivery_days": 6},
        {"price_kzt": "16000", "delivery_days": 1},
    ]


def decide(rows=None, **kwargs):
    return decide_automated_price(
        own_price_kzt=14000,
        safe_floor_kzt=13000,
        market_offers=rows or offers(),
        unit_cost_kzt=9000,
        offers_complete=True,
        **kwargs,
    )


def test_user_example_maximizes_profit_in_top_three():
    d = decide()
    assert d.target_price_kzt == Decimal("15999")
    assert d.write_allowed


def test_unknown_delivery_has_no_premium():
    rows = offers()
    rows[0]["delivery_days"] = None
    assert decide(rows).target_price_kzt == 13998


def test_faster_rivals_disallow_premium():
    rows = offers()
    rows[1]["delivery_days"] = 0
    assert decide(rows).target_price_kzt == 13998


def test_floor_never_undercut_when_top_three_impossible():
    d = decide_automated_price(
        own_price_kzt=14000,
        safe_floor_kzt=15000,
        market_offers=offers(),
        unit_cost_kzt=10000,
        offers_complete=True,
        target_position=1,
    )
    assert d.target_price_kzt == 15000 and d.status == "floor_limited"


def test_missing_coverage_blocks_writes():
    d = decide_automated_price(
        own_price_kzt=14000,
        safe_floor_kzt=13000,
        market_offers=offers(),
        unit_cost_kzt=9000,
    )
    assert not d.write_allowed and d.status == "automation_market_incomplete"


def test_price_cap_and_sales_experiment():
    assert decide(config={"maximum_price_kzt": 15000}).target_price_kzt == 15000
    assert decide(target_position=2).target_price_kzt == 13998


def test_logistics_jump_maximizes_net_profit():
    d = decide_automated_price(
        own_price_kzt=4900,
        safe_floor_kzt=4000,
        market_offers=[
            {"is_own": True, "price_kzt": 4900, "delivery_days": 1},
            {"price_kzt": 5100, "delivery_days": 1},
        ],
        unit_cost_kzt=3000,
        offers_complete=True,
    )
    assert d.target_price_kzt == 4999


def test_mode_restores_rules_and_blocks_old_editor(db_session):
    product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        save_automation(
            product.id, AutomationSettings(minimum_profit_kzt=2000), db_session
        )
        assert policy.pricing_mode == "automation" and policy.minimum_profit_kzt == 2000
        assert svc._policy_interval_seconds(policy) == 120
        with pytest.raises(HTTPException) as exc:
            upsert_fast_dumping_policy(
                product.id, FastDumpingPolicyUpsert(), db_session
            )
        assert exc.value.status_code == 409
        listing = read_automation(db_session)
        assert listing["items"][0]["automation"]["enabled"]
        save_automation(product.id, AutomationSettings(enabled=False), db_session)
        assert policy.pricing_mode == "manual" and policy.minimum_profit_kzt == 1000
        assert policy.undercut_step_kzt == 10 and policy.scan_interval_seconds == 600


def test_auto_scan_precedes_old_manual_backlog_and_retains_fairness(db_session):
    p, batch, manual, state = _seed_fast_product(db_session)
    with workspace_context(1):
        from backend.app.models import Product

        auto_product = Product(
            kaspi_product_id="other",
            merchant_sku="other",
            name="Other",
            sale_enabled=True,
        )
        db_session.add(auto_product)
        db_session.flush()
        db_session.add(InventoryBatch(product_id=auto_product.id, received_at=datetime.now(UTC),
                                      quantity_received=1, quantity_remaining=1, unit_cost=1000))
        auto = FastDumpingPolicy(
            product_id=auto_product.id, pricing_mode="automation", enabled=True
        )
        db_session.add(auto)
        db_session.flush()
        svc.ensure_state(db_session, policy=auto, workspace_id=1)
        old, _ = svc.queue_scan(
            db_session, policy=manual, workspace_id=1, reason="test"
        )
        new, _ = svc.queue_scan(db_session, policy=auto, workspace_id=1, reason="test")
        svc._AUTO_SCAN_STREAK[1] = 0
        assert svc.claim_job(db_session, workspace_id=1, agent_id="test").id == new.id
        # Next available ordinary job still proceeds when auto is already leased.
        assert svc.claim_job(db_session, workspace_id=1, agent_id="test").id == old.id


def test_sales_experiment_persists_while_market_price_is_stable(db_session):
    from backend.app.full_automation_service import observe_sales

    p, batch, policy, state = _seed_fast_product(db_session)
    policy.pricing_mode = "automation"
    policy.automation_config = {"observation_hours": 1}
    state.own_price_kzt = 14000
    state.automation_json = {
        "observed_price": "14000",
        "exposure_started_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "target_position": 3,
    }
    with workspace_context(1):
        assert (
            observe_sales(db_session, policy=policy, state=state)["target_position"]
            == 2
        )
        assert (
            observe_sales(db_session, policy=policy, state=state)["target_position"]
            == 2
        )


def test_complete_scan_auto_uses_fresh_full_market(db_session):
    p, batch, policy, state = _seed_fast_product(db_session)
    policy.pricing_mode = "automation"
    policy.minimum_profit_kzt = 0
    with workspace_context(1):
        job, _ = svc.queue_scan(
            db_session, policy=policy, workspace_id=1, reason="test"
        )
        job = svc.claim_job(db_session, workspace_id=1, agent_id="test")
        result = runtime._complete_scan_v2(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="test",
            lease_token=job.lease_token,
            succeeded=True,
            market_payload={
                "own_price_kzt": "14000",
                "competitor_price_kzt": "13999",
                "market_context_ok": True,
                "page_visible_price_kzt": "13999",
                "offers": offers(),
                "offers_complete": True,
                "seller_count": 4,
            },
        )
        assert result["queued_apply"]
        assert state.target_price_kzt == 15999
        assert state.automation_json["offers_complete"]


def test_pending_verification_blocks_mode_transition(db_session):
    product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = svc.queue_scan(
            db_session, policy=policy, workspace_id=1, reason="test"
        )
        job.status = "queued_verify"
        with pytest.raises(HTTPException) as exc:
            save_automation(product.id, AutomationSettings(), db_session)
        assert exc.value.status_code == 409
        assert job.status == "queued_verify" and policy.pricing_mode == "manual"


def test_other_workspace_cannot_enable_product(db_session):
    product, batch, policy, state = _seed_fast_product(db_session, workspace_id=3)
    with workspace_context(3):
        product.workspace_id = 3
        db_session.commit()
    with workspace_context(1):
        with pytest.raises(HTTPException) as exc:
            save_automation(product.id, AutomationSettings(), db_session)
        assert exc.value.status_code == 404
        assert policy.pricing_mode == "manual"


def test_four_priority_scan_slots_leave_fifth_for_ordinary_backlog(db_session):
    product, batch, manual, state = _seed_fast_product(db_session)
    with workspace_context(1):
        from backend.app.models import Product

        product2 = Product(
            kaspi_product_id="auto-fairness",
            merchant_sku="auto-fairness",
            name="Auto",
            sale_enabled=True,
        )
        db_session.add(product2)
        db_session.flush()
        auto = FastDumpingPolicy(product_id=product2.id, pricing_mode="automation")
        db_session.add(auto)
        db_session.flush()
        svc.ensure_state(db_session, policy=auto, workspace_id=1)
        old, _ = svc.queue_scan(
            db_session, policy=manual, workspace_id=1, reason="test"
        )
        new, _ = svc.queue_scan(db_session, policy=auto, workspace_id=1, reason="test")
        svc._AUTO_SCAN_STREAK[1] = 4
        assert svc.claim_job(db_session, workspace_id=1, agent_id="test").id == old.id


def test_supplier_auto_mode_uses_delivery_profit_strategy_and_rechecks_floor(
    db_session,
):
    from tests.test_fast_supplier_events import _seed_fast_supplier
    from backend.app.fast_dumping_supplier_pricing import _supplier_decision
    from backend.app.dumping_service import resolve_cost_source

    product, supplier_product, policy = _seed_fast_supplier(db_session)
    policy.pricing_mode = "automation"
    with workspace_context(1):
        state = svc.ensure_state(db_session, policy=policy, workspace_id=1)
        state.own_price_kzt = 14000
        state.offers_json = offers()
        state.automation_json = {"offers_complete": True}
        source = resolve_cost_source(
            db_session, product_id=product.id, inventory_first=True
        )
        d = _supplier_decision(state=state, policy=policy, source=source)
        assert Decimal(d["target_price_kzt"]) == 15999
        assert d["fulfillment_mode"] == "preorder" and d["preorder_days"] == 6
        supplier_product.current_price = Decimal("20000")
        db_session.flush()
        source = resolve_cost_source(
            db_session, product_id=product.id, inventory_first=True
        )
        d = _supplier_decision(state=state, policy=policy, source=source)
        assert Decimal(d["target_price_kzt"]) >= Decimal(d["safe_floor_kzt"])


def test_only_real_non_cancelled_kaspi_orders_restore_profit_priority(db_session):
    from backend.app.full_automation_service import observe_sales
    from backend.app.models import (
        MarketplaceAccount,
        MarketplaceOrder,
        MarketplaceOrderLine,
    )

    product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        account = MarketplaceAccount(
            workspace_id=1,
            provider="kaspi",
            external_account_id="sales-test",
            display_name="Sales",
        )
        db_session.add(account)
        db_session.flush()
        for status, quantity in [("cancelled", 50), ("returned", 50), ("delivered", 2)]:
            order = MarketplaceOrder(
                marketplace_account_id=account.id,
                external_order_id=status,
                status=status,
                total_amount=14000 * quantity,
                ordered_at=datetime.now(UTC) - timedelta(minutes=10),
            )
            db_session.add(order)
            db_session.flush()
            db_session.add(
                MarketplaceOrderLine(
                    marketplace_order_id=order.id,
                    external_line_id="1",
                    product_id=None,
                    merchant_sku=product.merchant_sku,
                    title=product.name,
                    quantity=quantity,
                    unit_price=14000,
                    line_total=14000 * quantity,
                )
            )
        db_session.flush()
        policy.pricing_mode = "automation"
        state.own_price_kzt = 14000
        state.automation_json = {
            "observed_price": "14000",
            "exposure_started_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "target_position": 1,
        }
        observed = observe_sales(db_session, policy=policy, state=state)
        assert observed["orders_units"] == 2 and observed["target_position"] == 3


def test_automation_supports_more_than_eight_cards(db_session):
    from backend.app.models import Product

    product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        for i in range(8):
            p = Product(
                kaspi_product_id=f"cap-{i}", merchant_sku=f"cap-{i}", name="Capacity"
            )
            db_session.add(p)
            db_session.flush()
            db_session.add(
                FastDumpingPolicy(
                    product_id=p.id, pricing_mode="automation", enabled=True
                )
            )
        db_session.flush()
        save_automation(product.id, AutomationSettings(), db_session)
        assert policy.pricing_mode == "automation"
        assert read_automation(db_session)["capacity"] is None


def test_owned_shop_cannot_become_an_undercut_target():
    rows = offers()
    rows[1].update(is_owned_group=True, delivery_days=1, price_kzt="13500")
    rows[2].update(is_owned_group=True, delivery_days=1, price_kzt="13600")
    rows.append({"price_kzt": "13700", "delivery_days": 1, "is_owned_group": True})
    d = decide(rows)
    assert d.target_price_kzt >= 14000 and d.status == "owned_peer_guard"


def test_auto_stable_price_does_not_create_merchant_retry_loop(db_session):
    product, batch, policy, state = _seed_fast_product(db_session)
    policy.pricing_mode = "automation"
    policy.minimum_profit_kzt = 0
    with workspace_context(1):
        job, _ = svc.queue_scan(
            db_session, policy=policy, workspace_id=1, reason="stable"
        )
        job = svc.claim_job(db_session, workspace_id=1, agent_id="test")
        rows = offers()
        rows[0]["price_kzt"] = "15999"
        result = runtime._complete_scan_v2(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="test",
            lease_token=job.lease_token,
            succeeded=True,
            market_payload={
                "own_price_kzt": "15999",
                "competitor_price_kzt": "13999",
                "market_context_ok": True,
                "offers": rows,
                "offers_complete": True,
            },
        )
        assert not result["queued_apply"]
        assert state.status == "watching" and state.active_job_id is None
        assert state.next_scan_at > datetime.now(UTC) + timedelta(seconds=60)
        assert job.status == "watching"


def test_no_competitor_still_protects_floor_and_never_raises_without_limit():
    d = decide_automated_price(
        own_price_kzt=14000,
        safe_floor_kzt=15000,
        unit_cost_kzt=10000,
        market_offers=[{"is_own": True, "price_kzt": "14000"}],
        offers_complete=True,
    )
    assert d.target_price_kzt == 15000 and d.write_allowed
    d = decide_automated_price(
        own_price_kzt=14000,
        safe_floor_kzt=13000,
        unit_cost_kzt=9000,
        market_offers=[{"is_own": True, "price_kzt": "14000"}],
        offers_complete=True,
    )
    assert d.target_price_kzt == 14000 and not d.write_allowed


def test_conflicting_maximum_price_and_floor_block_write():
    d = decide(config={"maximum_price_kzt": 12000})
    assert not d.write_allowed and d.status == "floor_limited"


def test_inline_settings_save_context_and_restore_manual_rules_atomically(db_session):
    product, _, policy, _ = _seed_fast_product(db_session)
    manual = FastDumpingPolicyUpsert(
        undercut_step_kzt=17, minimum_profit_kzt=1300,
        scan_interval_seconds=900, city_id="750000000", zone_id="ZONE2",
    )
    with workspace_context(1):
        save_automation(product.id, AutomationSettings(
            minimum_profit_kzt=2500, manual_policy=manual,
        ), db_session)
        assert policy.pricing_mode == "automation"
        assert policy.minimum_profit_kzt == 2500
        assert policy.city_id == "750000000" and policy.zone_id == "ZONE2"
        assert policy.automation_config["previous"]["undercut_step_kzt"] == 17
        assert "manual_policy" not in policy.automation_config
        save_automation(product.id, AutomationSettings(enabled=False), db_session)
        assert policy.minimum_profit_kzt == 1300
        assert policy.undercut_step_kzt == 17 and policy.scan_interval_seconds == 900
        save_automation(product.id, AutomationSettings(), db_session)
        save_automation(product.id, AutomationSettings(
            enabled=False, manual_policy=FastDumpingPolicyUpsert(
                minimum_profit_kzt=1700, undercut_step_kzt=23, enabled=False,
            ),
        ), db_session)
        assert policy.pricing_mode == "manual" and not policy.enabled
        assert policy.minimum_profit_kzt == 1700 and policy.undercut_step_kzt == 23


def test_removed_pages_redirect_to_fast_settings():
    from backend.app.ui import crm_dumping, full_automation_page
    for view in (crm_dumping, full_automation_page):
        response = view()
        assert response.status_code == 307
        assert response.headers["location"] == "/crm/fast-dumping"
