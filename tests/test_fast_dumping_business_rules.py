from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.fast_dumping_api import remove_fast_dumping_product
from backend.app.fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from backend.app.fast_dumping_pricing import decide_fast_price
from backend.app.fast_dumping_service import ensure_state, queue_scan
from backend.app.fast_dumping_xml_guard import _sync_product_inventory_to_feed
from backend.app.inventory_models import InventoryBatch, InventoryBatchType
from backend.app.models import Product
from backend.app.workspace_context import workspace_context


def test_fast_price_jumps_from_5927_to_4999_when_floor_allows() -> None:
    decision = decide_fast_price(
        own_price_kzt=Decimal("6000"),
        competitor_price_kzt=Decimal("5928"),
        safe_floor_kzt=Decimal("4200"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )

    assert decision.status == "logistics_jump"
    assert decision.target_price_kzt == Decimal("4999.00")
    assert decision.write_allowed is True
    assert "5000.00–5927.00" in decision.reason
    assert "4999.00" in decision.reason


def test_fast_price_does_not_cross_logistics_boundary_below_safe_floor() -> None:
    decision = decide_fast_price(
        own_price_kzt=Decimal("6000"),
        competitor_price_kzt=Decimal("5928"),
        safe_floor_kzt=Decimal("5100"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )

    assert decision.status == "ready"
    assert decision.target_price_kzt == Decimal("5927.00")
    assert decision.write_allowed is True


def test_logistics_jump_still_obeys_anomaly_limit() -> None:
    decision = decide_fast_price(
        own_price_kzt=Decimal("6000"),
        competitor_price_kzt=Decimal("5928"),
        safe_floor_kzt=Decimal("4200"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("10"),
    )

    assert decision.status == "price_anomaly"
    assert decision.write_allowed is False
    assert decision.target_price_kzt == Decimal("6000.00")


def test_fast_price_applies_same_rule_at_next_logistics_boundary() -> None:
    decision = decide_fast_price(
        own_price_kzt=Decimal("11600"),
        competitor_price_kzt=Decimal("11508"),
        safe_floor_kzt=Decimal("9000"),
        undercut_step_kzt=Decimal("1"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )

    assert decision.status == "logistics_jump"
    assert decision.target_price_kzt == Decimal("9999.00")


def _seed_fast_policy(db_session):
    with workspace_context(1):
        product = Product(
            kaspi_product_id="remove-fast-product",
            merchant_sku="remove-fast-product_sku",
            name="Remove from Fast",
            status="active",
            sale_enabled=True,
        )
        db_session.add(product)
        db_session.flush()
        policy = FastDumpingPolicy(product_id=product.id, enabled=True)
        db_session.add(policy)
        db_session.flush()
        state = ensure_state(db_session, policy=policy, workspace_id=1)
        db_session.commit()
    return product, policy, state


def test_remove_fast_product_keeps_catalog_product_and_stops_fast(db_session) -> None:
    product, policy, _state = _seed_fast_policy(db_session)
    with workspace_context(1):
        _job, queued = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test_remove",
        )
        db_session.commit()
        assert queued is True

        result = remove_fast_dumping_product(product.id, db=db_session)

    assert result == {
        "product_id": product.id,
        "removed": True,
        "kaspi_offer_changed": False,
    }
    assert db_session.get(Product, product.id) is not None
    assert db_session.scalar(
        select(FastDumpingPolicy).where(FastDumpingPolicy.product_id == product.id)
    ) is None
    assert db_session.scalar(
        select(FastDumpingState).where(FastDumpingState.product_id == product.id)
    ) is None
    assert db_session.scalar(
        select(FastDumpingJob).where(FastDumpingJob.product_id == product.id)
    ) is None


def test_remove_fast_product_refuses_while_realtime_write_is_leased(db_session) -> None:
    product, policy, state = _seed_fast_policy(db_session)
    with workspace_context(1):
        job = FastDumpingJob(
            policy_id=policy.id,
            product_id=product.id,
            status="leased_apply",
            agent_id="agent",
            lease_token="a" * 32,
        )
        db_session.add(job)
        db_session.flush()
        state.active_job_id = job.id
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            remove_fast_dumping_product(product.id, db=db_session)

    assert exc.value.status_code == 409
    assert db_session.get(FastDumpingPolicy, policy.id) is not None
    assert db_session.get(Product, product.id) is not None


def test_inventory_arrival_replaces_queued_preorder_apply_with_fresh_scan(
    db_session,
) -> None:
    product, policy, state = _seed_fast_policy(db_session)
    old_job = FastDumpingJob(
        workspace_id=1,
        policy_id=policy.id,
        product_id=product.id,
        status="queued_apply",
        decision_json={
            "fulfillment_mode": "preorder",
            "stock_count": 5,
            "preorder_days": 9,
        },
    )
    db_session.add(old_job)
    db_session.flush()
    state.active_job_id = old_job.id
    state.inventory_on_hand = 0
    state.desired_stock_count = 5
    db_session.add(
        InventoryBatch(
            product_id=product.id,
            received_at=datetime.now(UTC),
            batch_type=InventoryBatchType.PURCHASE.value,
            is_received=True,
            quantity_received=3,
            quantity_remaining=3,
            unit_cost=Decimal("5805"),
        )
    )
    db_session.flush()

    result = _sync_product_inventory_to_feed(
        db_session,
        product_id=product.id,
        reason="inventory_batch_created",
    )
    db_session.flush()

    db_session.refresh(old_job)
    db_session.refresh(state)
    replacement = db_session.get(FastDumpingJob, state.active_job_id)
    assert result == {
        "stock_count": 3,
        "xml_state": "fast_realtime_owned",
        "supplier_job_id": None,
    }
    assert old_job.status == "cancelled"
    assert replacement is not None
    assert replacement.id != old_job.id
    assert replacement.status == "queued_scan"
    assert replacement.reason == "inventory_event:inventory_batch_created"
    assert state.inventory_on_hand == 3


def test_inventory_exhaustion_replaces_queued_fifo_apply_with_fresh_scan(
    db_session,
) -> None:
    product, policy, state = _seed_fast_policy(db_session)
    old_job = FastDumpingJob(
        workspace_id=1,
        policy_id=policy.id,
        product_id=product.id,
        status="queued_apply",
        decision_json={
            "fulfillment_mode": "inventory",
            "stock_count": 1,
            "preorder_days": 0,
        },
    )
    db_session.add(old_job)
    db_session.flush()
    state.active_job_id = old_job.id
    state.inventory_on_hand = 1
    state.desired_stock_count = 1

    result = _sync_product_inventory_to_feed(
        db_session,
        product_id=product.id,
        reason="order_inventory_allocated",
    )
    db_session.flush()

    db_session.refresh(old_job)
    db_session.refresh(state)
    replacement = db_session.get(FastDumpingJob, state.active_job_id)
    assert result["stock_count"] == 0
    assert old_job.status == "cancelled"
    assert replacement is not None
    assert replacement.id != old_job.id
    assert replacement.status == "queued_scan"
    assert state.inventory_on_hand == 0


def test_fast_dumping_ui_connects_remove_button_to_delete_endpoint() -> None:
    javascript = (
        Path(__file__).resolve().parents[1]
        / "backend/app/static/fast-dumping.js"
    ).read_text(encoding="utf-8")

    assert 'document.querySelector("#edit-remove")' in javascript
    assert '`/api/fast-dumping/products/${productId}`' in javascript
    assert 'method:"DELETE"' in javascript
