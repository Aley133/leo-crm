from datetime import UTC, datetime, timedelta

import pytest

from backend.app import fast_dumping_service as svc
from backend.app.product_test_models import ProductTestItem  # noqa: F401
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _claim, _market, _seed_fast_product


@pytest.mark.parametrize("blocked", [None, "manual", "context", "identity", "stock", "missing_own", "price"])
def test_exact_live_offer_recovers_only_nonmanual_sale_flag(db_session, blocked):
    product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        product.sale_enabled = False
        product.sale_state_overridden = blocked == "manual"
        job, _ = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason="manual")
        db_session.commit()
        job = _claim(db_session, 1)
        market = _market(own="20000", competitor="21000")
        market["offers"][0]["own_match"] = "merchant_uid"
        if blocked == "context":
            market["market_context_ok"] = False
        elif blocked == "identity":
            market["offers"][0]["own_match"] = None
        elif blocked == "stock":
            batch.quantity_remaining = 0
        elif blocked == "missing_own":
            market["offers"] = market["offers"][1:]
        elif blocked == "price":
            market["offers"][0]["price_kzt"] = "19999"
        result = svc.complete_scan(db_session, workspace_id=1, job_id=job.id,
            agent_id="fast-agent", lease_token=job.lease_token,
            succeeded=True, market_payload=market)
        assert product.sale_enabled is (blocked is None)
        assert product.sale_state_overridden is (blocked == "manual")
        assert result["queued_apply"] is (blocked is None)
        if blocked is not None:
            assert state.status == "paused"


@pytest.mark.parametrize("manual", [False, True])
def test_only_imported_sale_disable_is_scheduled_for_recheck(db_session, manual):
    product, _, _, state = _seed_fast_product(db_session)
    with workspace_context(1):
        product.sale_enabled = False
        product.sale_state_overridden = manual
        state.next_scan_at = datetime.now(UTC) - timedelta(minutes=1)
        assert svc.schedule_due_scans(db_session, workspace_id=1,
            recover_inventory_transitions=False) == (0 if manual else 1)
        assert product.sale_enabled is False  # Scheduling alone is not proof.


def test_sale_recovery_uses_shared_inventory_owner(db_session):
    from backend.app.models import Product
    from backend.app.fast_dumping_models import FastDumpingPolicy
    owner, _, _, _ = _seed_fast_product(db_session)
    with workspace_context(1):
        alias = Product(name="Shared offer", kaspi_product_id="shared-offer",
            merchant_sku="shared-sku", inventory_owner_product_id=owner.id,
            sale_enabled=False, sale_state_overridden=False)
        db_session.add(alias)
        db_session.flush()
        policy = FastDumpingPolicy(product_id=alias.id, enabled=True, minimum_profit_kzt=1000)
        db_session.add(policy)
        db_session.flush()
        state = svc.ensure_state(db_session, policy=policy, workspace_id=1)
        job, _ = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason="manual")
        db_session.commit()
        claimed = _claim(db_session, 1)
        assert claimed.id == job.id
        market = _market(own="20000", competitor="21000")
        market["offers"][0]["own_match"] = "merchant_sku"
        svc.complete_scan(db_session, workspace_id=1, job_id=job.id,
            agent_id="fast-agent", lease_token=job.lease_token,
            succeeded=True, market_payload=market)
        assert alias.sale_enabled and state.inventory_on_hand == 4


@pytest.mark.parametrize("status, reason, expected", [
    ("queued_scan", "scheduled", "manual"),
    ("queued_scan", "inventory_priority:arrival", "inventory_priority:arrival"),
    ("leased_scan", "scheduled", "scheduled"),
    ("queued_verify", "scheduled", "scheduled"),
])
def test_manual_check_promotes_pending_scan_without_recreating_job(db_session, status, reason, expected):
    _, _, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        original, _ = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason=reason)
        original.status = status
        token = original.lease_token = "keep-this-lease" if status == "leased_scan" else None
        again, created = svc.queue_scan(db_session, policy=policy, workspace_id=1, reason="manual")
        assert not created and again.id == original.id and state.active_job_id == original.id
        assert again.reason == expected and again.status == status and again.lease_token == token
