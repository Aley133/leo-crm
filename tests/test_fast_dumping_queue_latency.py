from datetime import UTC, datetime, timedelta

import pytest

from backend.app import fast_dumping_service as svc
from backend.app.fast_dumping_models import FastDumpingJob, FastDumpingPolicy
from backend.app.models import Product
from backend.app.product_test_models import ProductTestItem  # noqa: F401
from backend.app.workspace_context import workspace_context


def seed(db, suffix, status="queued_scan", reason="scheduled"):
    product = Product(workspace_id=1, kaspi_product_id=suffix,
                      merchant_sku=suffix, name=suffix, sale_enabled=True)
    db.add(product)
    db.flush()
    policy = FastDumpingPolicy(workspace_id=1, product_id=product.id)
    db.add(policy)
    db.flush()
    state = svc.ensure_state(db, policy=policy, workspace_id=1)
    job, _ = svc.queue_scan(db, policy=policy, workspace_id=1, reason=reason)
    job.status = status
    db.flush()
    return job, state, policy


@pytest.fixture(autouse=True)
def isolated_counters(monkeypatch):
    monkeypatch.setattr(svc, "_AUTO_SCAN_STREAK", {})
    monkeypatch.setattr(svc, "_NON_SCAN_STREAK", {})
    monkeypatch.setattr(svc, "_reserve_inventory_recovery", lambda _: False)


@pytest.mark.parametrize("reason", ["policy_saved", "automation_mode_changed", "product_test_auto_enroll"])
def test_new_connection_precedes_old_periodic_and_verification_backlog(db_session, reason):
    with workspace_context(1):
        old, _, _ = seed(db_session, "old")
        verify, _, _ = seed(db_session, "verify", "queued_verify")
        new, _, _ = seed(db_session, "new", reason=reason)
        claimed = svc.claim_job(db_session, workspace_id=1, agent_id="worker")
        assert claimed.id == new.id
        assert old.status == "queued_scan"
        assert verify.status == "queued_verify"


def test_continuous_confirmations_cannot_starve_scans(db_session):
    with workspace_context(1):
        scan, state, _ = seed(db_session, "waiting")
        state.last_scanned_at = datetime.now(UTC) - timedelta(hours=3)
        for index in range(5):
            seed(db_session, f"write-{index}", "queued_verify")
        for _ in range(3):
            assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").status == "leased_verify"
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").id == scan.id
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").status == "leased_verify"


def test_detached_queued_job_does_not_delay_valid_work(db_session):
    with workspace_context(1):
        orphan, orphan_state, _ = seed(db_session, "orphan", "queued_verify")
        orphan_state.active_job_id = None
        orphan_state.next_scan_at = datetime.now(UTC) + timedelta(hours=1)
        valid, _, _ = seed(db_session, "valid")
        db_session.flush()
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").id == valid.id
        assert orphan.status == "queued_verify"


def test_old_manual_scan_gets_a_turn_during_continuous_rocket_traffic(db_session):
    with workspace_context(1):
        old, state, _ = seed(db_session, "old-manual")
        old.created_at = datetime.now(UTC) - timedelta(hours=3)
        state.last_scanned_at = datetime.now(UTC) - timedelta(hours=4)
        auto, _, policy = seed(db_session, "rocket")
        policy.pricing_mode = "automation"
        db_session.flush()
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").id == auto.id
        auto2, _, policy2 = seed(db_session, "rocket-2")
        policy2.pricing_mode = "automation"
        db_session.flush()
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").id == old.id
        assert auto2.status == "queued_scan"


def test_mode_switch_cancels_only_scan_and_queues_priority_refresh(db_session):
    from backend.app.full_automation_api import AutomationSettings, save_automation
    with workspace_context(1):
        old, state, policy = seed(db_session, "switch")
        save_automation(policy.product_id, AutomationSettings(), db_session)
        fresh = db_session.get(FastDumpingJob, state.active_job_id)
        assert policy.pricing_mode == "automation"
        assert old.status == "cancelled"
        assert fresh.reason == "automation_mode_changed"
        assert svc.claim_job(db_session, workspace_id=1, agent_id="worker").id == fresh.id
