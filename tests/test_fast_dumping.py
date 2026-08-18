from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, select

from backend.app.dumping_service import calculate_safe_floor
from backend.app.fast_dumping_agent_api import _validate_workspace_merchant
from backend.app.fast_dumping_api import (
    FastDumpingPolicyUpsert,
    list_fast_dumping_products,
    read_fast_dumping_offers,
    run_fast_dumping_now,
)
from backend.app.fast_dumping_models import (
    FastDumpingJob,
    FastDumpingPolicy,
    FastDumpingState,
)
from backend.app.fast_dumping_pricing import decide_fast_price
from backend.app.fast_dumping_service import (
    claim_job,
    complete_apply,
    complete_scan,
    complete_verification,
    ensure_state,
    prepare_apply,
    prune_fast_dumping_history,
    queue_scan,
    recover_expired_leases,
    serialize_claimed_job,
)
from backend.app.inventory_models import InventoryBatch
from backend.app.models import MarketplaceAccount, Product
from backend.app.workspace_context import workspace_context
from backend.app.workspace_models import KaspiAccountCredential, Workspace
from tools.kaspi_fast_dumping_scanner import (
    _delivery_days,
    _merchant_id,
    _own_match,
    _page_visible_price,
    _select_delivery_aware_competitor,
)


ROOT = Path(__file__).resolve().parents[1]


def test_scanner_recognizes_nested_merchant_uid_and_safe_sku_fallback() -> None:
    nested = {"merchant": {"merchantUid": "  BAR-WORK  "}, "merchantSku": "SKU-1"}
    assert _merchant_id(nested) == "BAR-WORK"
    assert _own_match(
        nested,
        own_merchant_id="bar-work",
        own_merchant_sku="SKU-1",
    ) == "merchant_uid"

    missing_uid = {"merchantSKU": "SKU-1"}
    assert _own_match(
        missing_uid,
        own_merchant_id="bar-work",
        own_merchant_sku="SKU-1",
    ) == "merchant_sku"

    assert _own_match(
        {},
        own_merchant_id="",
        own_merchant_sku=None,
    ) is None

    conflicting_uid = {"merchantId": "competitor", "merchantSku": "SKU-1"}
    assert _own_match(
        conflicting_uid,
        own_merchant_id="bar-work",
        own_merchant_sku="SKU-1",
    ) is None


def test_scanner_normalizes_real_kaspi_delivery_date_and_ignores_pickup_steps() -> None:
    today = date(2026, 8, 13)

    assert _delivery_days(
        {
            "delivery": "2026-08-15T18:00:00.000+00:00",
            "deliveryMovedBySlot": False,
            "kdPickupDate": "2026-08-14T15:00:00.000+00:00",
            "kaspiDelivery": True,
        },
        today=today,
    ) == 2
    assert _delivery_days(
        {
            "deliverySteps": {"PO": "2026-08-15T19:00:00.000+00:00"},
            "kdPickupDate": "2026-08-14T15:00:00.000+00:00",
        },
        today=today,
    ) is None
    assert _delivery_days({"deliveryText": "доставка 5–7 дней"}, today=today) == 5
    timestamp_ms = int(
        datetime(2026, 8, 15, 18, tzinfo=UTC).timestamp() * 1000
    )
    assert _delivery_days({"delivery": timestamp_ms}, today=today) == 2
    assert _delivery_days({"delivery": str(timestamp_ms)}, today=today) == 2


def test_scanner_skips_slightly_cheaper_slow_offer_for_faster_delivery() -> None:
    today = date(2026, 8, 13)
    own = {
        "price": "20000",
        "delivery": "2026-08-13T19:00:00.000+00:00",
    }
    slow_cheaper = {
        "price": "19500",
        "delivery": "2026-08-18T19:00:00.000+00:00",
    }
    acceptable = {
        "price": "19700",
        "delivery": "2026-08-15T19:00:00.000+00:00",
    }

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [slow_cheaper, acceptable],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=5,
        today=today,
    )

    assert selected is acceptable
    assert assessments[id(slow_cheaper)].ignored is True
    assert assessments[id(slow_cheaper)].price_gap_kzt == Decimal("500")
    assert assessments[id(slow_cheaper)].delivery_gap_days == 5
    assert "Исключён из ценового ориентира" in assessments[id(slow_cheaper)].reason
    assert assessments[id(acceptable)].ignored is False
    assert "быстрее только на 2 дн." in assessments[id(acceptable)].reason


def test_scanner_keeps_competitor_when_threshold_or_delivery_is_unknown() -> None:
    today = date(2026, 8, 13)
    own = {"price": "20000", "delivery": "2026-08-13T19:00:00+00:00"}
    too_cheap = {"price": "19499", "delivery": "2026-08-20T19:00:00+00:00"}
    unknown_delivery = {"price": "19500", "kaspiDelivery": True}

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [too_cheap, unknown_delivery],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=5,
        today=today,
    )

    assert selected is too_cheap
    assert assessments[id(too_cheap)].ignored is False
    assert "превышает допустимую доплату" in assessments[id(too_cheap)].reason
    assert assessments[id(unknown_delivery)].ignored is False
    assert "срок доставки конкурента не распознан" in assessments[id(unknown_delivery)].reason


def test_delivery_filter_never_raises_price_above_the_protected_premium() -> None:
    today = date(2026, 8, 13)
    own = {"price": "20000", "delivery": "2026-08-13T19:00:00+00:00"}
    slow_cheaper = {"price": "19500", "delivery": "2026-08-18T19:00:00+00:00"}
    expensive = {"price": "22000", "delivery": "2026-08-14T19:00:00+00:00"}

    selected, assessments = _select_delivery_aware_competitor(
        own,
        [slow_cheaper, expensive],
        max_price_premium_kzt=500,
        min_delivery_advantage_days=5,
        today=today,
    )

    assert assessments[id(slow_cheaper)].ignored is True
    assert assessments[id(expensive)].ignored is False
    assert selected is None

def _seed_fast_product(db_session, *, workspace_id: int = 1, quantity: int = 4):
    with workspace_context(workspace_id):
        product = Product(
            kaspi_product_id=f"fast-product-{workspace_id}",
            merchant_sku=f"fast-product-{workspace_id}_merchant-sku",
            name=f"Fast product {workspace_id}",
            brand="LEO",
            status="active",
            sale_enabled=True,
        )
        db_session.add(product)
        db_session.flush()
        batch = InventoryBatch(
            product_id=product.id,
            received_at=datetime.now(UTC),
            quantity_received=quantity,
            quantity_remaining=quantity,
            unit_cost=Decimal("10000"),
            source_name=f"Warehouse {workspace_id}",
        )
        policy = FastDumpingPolicy(
            product_id=product.id,
            enabled=True,
            minimum_profit_kzt=Decimal("1000"),
            undercut_step_kzt=10,
            allow_price_raise=True,
            max_undercut_gap_percent=Decimal("35"),
            scan_interval_seconds=600,
            city_id="196220100",
            zone_id="Magnum_ZONE1",
        )
        db_session.add_all((batch, policy))
        db_session.flush()
        state = ensure_state(
            db_session,
            policy=policy,
            workspace_id=workspace_id,
        )
        db_session.commit()
        return product, batch, policy, state


def _market(*, own: str, competitor: str, context_ok: bool = True) -> dict:
    return {
        "product_name": "Fast product model",
        "product_brand": "LEO",
        "own_price_kzt": own,
        "competitor_price_kzt": competitor,
        "competitor_name": "Competitor",
        "own_position": 2,
        "seller_count": 5,
        "product_url": "https://kaspi.kz/shop/p/fast-product-123/",
        "offers": [
            {
                "merchant_id": "own",
                "merchant_name": "BARWORK",
                "is_own": True,
                "price_kzt": own,
                "used_for_dumping": False,
            },
            {
                "merchant_id": "external",
                "merchant_name": "Competitor",
                "is_own": False,
                "price_kzt": competitor,
                "used_for_dumping": True,
            },
        ],
        "page_visible_price_kzt": competitor,
        "market_context_ok": context_ok,
        "market_context_reason": "confirmed" if context_ok else "mismatch",
    }


def _claim(db_session, workspace_id: int, agent_id: str = "fast-agent"):
    db_session.info["include_all_workspaces"] = True
    try:
        with workspace_context(workspace_id):
            job = claim_job(
                db_session,
                workspace_id=workspace_id,
                agent_id=agent_id,
            )
            db_session.commit()
            return job
    finally:
        db_session.info.pop("include_all_workspaces", None)


def test_fast_price_keeps_crm_floor_and_lab_safety_guards() -> None:
    ready = decide_fast_price(
        own_price_kzt=Decimal("20000"),
        competitor_price_kzt=Decimal("19800"),
        safe_floor_kzt=Decimal("17000"),
        undercut_step_kzt=Decimal("10"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )
    limited = decide_fast_price(
        own_price_kzt=Decimal("17000"),
        competitor_price_kzt=Decimal("16900"),
        safe_floor_kzt=Decimal("17000"),
        undercut_step_kzt=Decimal("10"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )
    anomaly = decide_fast_price(
        own_price_kzt=Decimal("19800"),
        competitor_price_kzt=Decimal("9900"),
        safe_floor_kzt=Decimal("9000"),
        undercut_step_kzt=Decimal("10"),
        allow_price_raise=True,
        max_undercut_gap_percent=Decimal("35"),
    )

    assert ready.target_price_kzt == Decimal("19790.00")
    assert limited.status == "floor_limited"
    assert limited.target_price_kzt == Decimal("17000.00")
    assert anomaly.status == "price_anomaly"
    assert anomaly.write_allowed is False


def test_fast_dumping_full_scan_prepare_apply_cycle(db_session) -> None:
    product, _batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, created = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    assert created is True

    leased_scan = _claim(db_session, 1)
    assert leased_scan.id == job.id
    assert leased_scan.status == "leased_scan"
    with workspace_context(1):
        claimed_payload = serialize_claimed_job(
            db_session,
            job=leased_scan,
            workspace_id=1,
        )
    assert claimed_payload["delivery_price_premium_kzt"] == 500
    assert claimed_payload["delivery_advantage_days"] == 5
    with workspace_context(1):
        result = complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_scan.lease_token,
            succeeded=True,
            market_payload=_market(own="20000", competitor="19800"),
        )
        db_session.commit()
    assert result["queued_apply"] is True

    leased_apply = _claim(db_session, 1)
    assert leased_apply.status == "leased_apply"
    with workspace_context(1):
        prepared = prepare_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
        )
        db_session.commit()
    assert prepared["ready"] is True
    assert prepared["stock_count"] == 4
    assert prepared["sku"] == product.merchant_sku

    with workspace_context(1):
        completed = complete_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
            write_payload={
                "accepted": True,
                "verified": True,
                "status_code": 200,
                "operation_id": "operation-1",
                "latency_seconds": 6,
                "observed_own_price_kzt": prepared["target_price_kzt"],
            },
        )
        db_session.commit()
    db_session.refresh(state)
    assert completed == {"status": "applied", "verified": True}
    assert state.active_job_id is None
    assert state.last_operation_id == "operation-1"
    assert state.inventory_on_hand == 4


def test_fast_dumping_cancels_stale_stock_before_write(db_session) -> None:
    _product, batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased_scan = _claim(db_session, 1)
    with workspace_context(1):
        complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_scan.lease_token,
            succeeded=True,
            market_payload=_market(own="20000", competitor="19800"),
        )
        db_session.commit()
    leased_apply = _claim(db_session, 1)
    batch.quantity_remaining = 3
    db_session.commit()

    with workspace_context(1):
        prepared = prepare_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
        )
        db_session.commit()
    db_session.refresh(state)
    assert prepared["ready"] is False
    assert prepared["stale"] is True
    assert state.active_job_id is None
    assert state.inventory_on_hand == 3


def test_accepted_write_is_verified_once_after_configured_interval(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased_scan = _claim(db_session, 1)
    with workspace_context(1):
        complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_scan.lease_token,
            succeeded=True,
            market_payload=_market(own="20000", competitor="19800"),
        )
        db_session.commit()
    leased_apply = _claim(db_session, 1)
    with workspace_context(1):
        prepared = prepare_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
        )
        assert prepared["ready"]
        completed = complete_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
            write_payload={
                "accepted": True,
                "verified": False,
                "status_code": 200,
                "operation_id": "unknown-operation",
                "latency_seconds": 120,
            },
        )
        db_session.commit()
    db_session.refresh(state)
    db_session.refresh(job)
    assert completed["verification_scheduled"] is True
    assert state.status == "verifying"
    assert state.automatic_writes_paused is False
    assert state.active_job_id == job.id
    assert job.status == "queued_verify"
    assert job.not_before_at is not None
    assert job.not_before_at >= state.last_applied_at + timedelta(seconds=600)

    # A claim before the 10-minute verification deadline must not touch Kaspi.
    assert _claim(db_session, 1) is None

    job.not_before_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    leased_verify = _claim(db_session, 1)
    assert leased_verify.status == "leased_verify"
    with workspace_context(1):
        verified = complete_verification(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_verify.lease_token,
            observed_own_price_kzt=prepared["target_price_kzt"],
        )
        db_session.commit()
    db_session.refresh(state)
    assert verified == {"status": "applied", "verified": True}
    assert state.active_job_id is None
    assert state.automatic_writes_paused is False


def test_failed_delayed_verification_schedules_fresh_scan_without_latch(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased_scan = _claim(db_session, 1)
    with workspace_context(1):
        complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_scan.lease_token,
            succeeded=True,
            market_payload=_market(own="20000", competitor="19800"),
        )
        db_session.commit()
    leased_apply = _claim(db_session, 1)
    with workspace_context(1):
        prepared = prepare_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
        )
        complete_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
            write_payload={
                "accepted": True,
                "verified": False,
                "status_code": 200,
                "operation_id": "unknown-operation",
            },
        )
        db_session.commit()
    job.not_before_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    leased_verify = _claim(db_session, 1)
    with workspace_context(1):
        result = complete_verification(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_verify.lease_token,
            observed_own_price_kzt="20000",
        )
        db_session.commit()
    db_session.refresh(state)
    db_session.refresh(job)
    assert result["status"] == "verification_retry"
    assert result["verified"] is False
    assert job.status == "verification_missed"
    assert state.status == "verification_retry"
    assert state.own_price_kzt == Decimal("20000")
    assert state.automatic_writes_paused is False
    assert state.pause_reason is None
    assert state.next_scan_at is not None
    assert result["retry_at"] >= datetime.now(UTC) + timedelta(seconds=590)


def test_expired_verification_lease_schedules_fresh_scan_without_latch(db_session) -> None:
    product, _batch, policy, state = _seed_fast_product(db_session)
    job = FastDumpingJob(
        workspace_id=1,
        policy_id=policy.id,
        product_id=product.id,
        status="leased_verify",
        agent_id="fast-agent",
        lease_token="expired-verification-token",
        lease_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(job)
    db_session.flush()
    state.active_job_id = job.id
    db_session.commit()

    checked_at = datetime.now(UTC)
    db_session.info["include_all_workspaces"] = True
    try:
        with workspace_context(1):
            recovered = recover_expired_leases(
                db_session,
                workspace_id=1,
                now=checked_at,
            )
            db_session.commit()
    finally:
        db_session.info.pop("include_all_workspaces", None)

    db_session.refresh(job)
    db_session.refresh(state)
    assert recovered == 1
    assert job.status == "verification_failed"
    assert state.status == "verification_retry"
    assert state.automatic_writes_paused is False
    retry_at = state.next_scan_at
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert retry_at == checked_at + timedelta(seconds=600)


def test_missing_own_offer_pauses_repeated_fast_scans(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased_scan = _claim(db_session, 1)

    market = _market(own="20000", competitor="19800")
    market["own_price_kzt"] = None
    market["offers"] = [
        offer for offer in market["offers"] if not offer.get("is_own")
    ]
    with workspace_context(1):
        result = complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_scan.lease_token,
            succeeded=True,
            market_payload=market,
        )
        db_session.commit()

    db_session.refresh(state)
    assert result["status"] == "own_offer_missing"
    assert state.automatic_writes_paused is True
    assert state.next_scan_at is None
    assert "Merchant UID" in state.pause_reason


def test_floor_limited_product_is_exposed_for_inline_threshold_edit(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    floor = calculate_safe_floor(
        unit_cost_kzt=Decimal("10000"),
        minimum_profit_kzt=Decimal("1000"),
    )
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased = _claim(db_session, 1)
    with workspace_context(1):
        result = complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased.lease_token,
            succeeded=True,
            market_payload=_market(
                own=format(floor, "f"),
                competitor=format(floor - Decimal("100"), "f"),
            ),
        )
        db_session.commit()
        payload = list_fast_dumping_products(db_session)
    db_session.refresh(state)
    assert result["status"] == "floor_limited"
    assert state.status == "floor_limited"
    assert payload["summary"]["floor_limited"] == 1
    assert payload["items"][0]["current_safe_floor_kzt"] == floor
    assert "offers" not in payload["items"][0]["state"]
    assert payload["items"][0]["state"]["offers_count"] == 2
    with workspace_context(1):
        offers = read_fast_dumping_offers(state.product_id, db_session)
    assert len(offers["offers"]) == 2


def test_public_card_price_guard_ignores_installment_fragments() -> None:
    assert _page_visible_price('<meta property="product:price:amount" content="8221">') == Decimal("8221")
    assert _page_visible_price("<div>В рассрочку 2 741 ₸</div>") is None


def test_fast_agent_merchant_uid_must_match_selected_workspace(db_session) -> None:
    db_session.info["include_all_workspaces"] = True
    try:
        workspace = Workspace(id=3, name="LeoXpress", slug="leoxpress", is_active=True)
        account = MarketplaceAccount(
            workspace_id=3,
            provider="kaspi",
            external_account_id="merchant-leo",
            display_name="LeoXpress",
            timezone="Asia/Almaty",
        )
        db_session.add_all((workspace, account))
        db_session.flush()
        db_session.add(
            KaspiAccountCredential(
                workspace_id=3,
                marketplace_account_id=account.id,
                partner_id="merchant-leo",
                api_token_encrypted="not-needed-for-this-contract",
            )
        )
        db_session.commit()

        _validate_workspace_merchant(
            db_session,
            workspace_id=3,
            merchant_uid="merchant-leo",
        )
        try:
            _validate_workspace_merchant(
                db_session,
                workspace_id=3,
                merchant_uid="merchant-barwork",
            )
        except ValueError as exc:
            assert "не совпадает" in str(exc)
        else:
            raise AssertionError("Cross-workspace Merchant UID was accepted")
    finally:
        db_session.info.pop("include_all_workspaces", None)


def test_fast_dumping_list_defers_heavy_offer_diagnostics(db_session) -> None:
    _product, _batch, _policy, state = _seed_fast_product(db_session)
    state.offers_json = [{"merchant_name": "seller", "price_kzt": "10000"}] * 50
    state.offers_count = 50
    db_session.commit()
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        with workspace_context(1):
            payload = list_fast_dumping_products(db_session)
    finally:
        event.remove(bind, "before_cursor_execute", capture)

    assert payload["items"][0]["state"]["offers_count"] == 50
    assert "offers" not in payload["items"][0]["state"]
    state_selects = [sql for sql in statements if "fast_dumping_states" in sql]
    assert state_selects
    assert all("offers_json" not in sql for sql in state_selects)


def test_fast_dumping_accepts_only_configured_safe_intervals() -> None:
    for seconds in (300, 600, 900, 1800, 2100, 3600):
        assert (
            FastDumpingPolicyUpsert(scan_interval_seconds=seconds).scan_interval_seconds
            == seconds
        )
    for unsafe in (10, 1200, 7200):
        try:
            FastDumpingPolicyUpsert(scan_interval_seconds=unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsupported Fast Dumping interval was accepted: {unsafe}")


def test_fast_dumping_defaults_to_safe_kaspi_city() -> None:
    assert FastDumpingPolicyUpsert().city_id == "196220100"
    city_column = FastDumpingPolicy.__table__.c.city_id
    assert city_column.default.arg == "196220100"
    assert "196220100" in str(city_column.server_default.arg)


def test_fast_dumping_validates_delivery_advantage_thresholds() -> None:
    defaults = FastDumpingPolicyUpsert()
    custom = FastDumpingPolicyUpsert(
        delivery_price_premium_kzt=750,
        delivery_advantage_days=7,
    )

    assert defaults.delivery_price_premium_kzt == 500
    assert defaults.delivery_advantage_days == 5
    assert custom.delivery_price_premium_kzt == 750
    assert custom.delivery_advantage_days == 7
    for invalid in (
        {"delivery_price_premium_kzt": -1},
        {"delivery_advantage_days": 0},
        {"delivery_advantage_days": 31},
    ):
        try:
            FastDumpingPolicyUpsert(**invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe delivery threshold was accepted: {invalid}")


def test_delivery_advantage_holds_price_when_all_cheaper_offers_are_slow(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="test",
        )
        db_session.commit()
    leased = _claim(db_session, 1)
    market = _market(own="20000", competitor="19500")
    market["competitor_price_kzt"] = None
    market["competitor_name"] = None
    market["delivery_filtered_count"] = 1
    market["delivery_selection_reason"] = (
        "Цена сохранена: более дешёвый оффер доставляет на 5 дней позже."
    )
    market["offers"][1]["used_for_dumping"] = False
    market["offers"][1]["ignored_reason"] = market["delivery_selection_reason"]

    with workspace_context(1):
        result = complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased.lease_token,
            succeeded=True,
            market_payload=market,
        )
        db_session.commit()

    db_session.refresh(state)
    assert result["status"] == "delivery_advantage"
    assert result["queued_apply"] is False
    assert state.target_price_kzt == Decimal("20000")
    assert "5 дней позже" in state.status_reason


def test_recent_price_write_blocks_manual_scan_from_queuing_apply(db_session) -> None:
    _product, _batch, policy, state = _seed_fast_product(db_session)
    state.last_applied_at = datetime.now(UTC)
    db_session.commit()
    with workspace_context(1):
        job, _ = queue_scan(
            db_session,
            policy=policy,
            workspace_id=1,
            reason="manual",
        )
        db_session.commit()
    leased = _claim(db_session, 1)
    with workspace_context(1):
        result = complete_scan(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased.lease_token,
            succeeded=True,
            market_payload=_market(own="20000", competitor="19800"),
        )
        db_session.commit()
    db_session.refresh(state)
    assert result["queued_apply"] is False
    assert result["status"] == "cooldown"
    assert state.status == "cooldown"
    assert state.active_job_id is None
    assert state.next_scan_at >= state.last_applied_at + timedelta(seconds=600)


def test_manual_run_respects_next_scan_deadline(db_session) -> None:
    product, _batch, _policy, state = _seed_fast_product(db_session)
    state.next_scan_at = datetime.now(UTC) + timedelta(minutes=10)
    db_session.commit()
    with workspace_context(1):
        result = run_fast_dumping_now(product.id, db_session)

    assert result["status"] == "cooldown"
    assert result["queued"] is False
    assert state.active_job_id is None


def test_fast_dumping_completed_job_history_is_bounded(db_session) -> None:
    product, _batch, policy, state = _seed_fast_product(db_session)
    now = datetime.now(UTC)
    jobs = [
        FastDumpingJob(
            workspace_id=1,
            policy_id=policy.id,
            product_id=product.id,
            status="watching",
            completed_at=now + timedelta(seconds=index),
            market_json={"offers": [{"index": index}]},
        )
        for index in range(15)
    ]
    active = FastDumpingJob(
        workspace_id=1,
        policy_id=policy.id,
        product_id=product.id,
        status="queued_scan",
    )
    db_session.add_all([*jobs, active])
    db_session.flush()
    state.active_job_id = active.id
    db_session.commit()

    with workspace_context(1):
        removed = prune_fast_dumping_history(
            db_session,
            workspace_id=1,
            per_product_limit=10,
        )
        db_session.commit()

    remaining_completed = db_session.scalars(
        select(FastDumpingJob).where(
            FastDumpingJob.workspace_id == 1,
            FastDumpingJob.product_id == product.id,
            FastDumpingJob.completed_at.is_not(None),
        )
    ).all()
    assert removed == 5
    assert len(remaining_completed) == 10
    assert db_session.get(FastDumpingJob, active.id) is not None


def test_fast_dumping_ui_and_agent_are_separate_from_ordinary_dumping() -> None:
    html = (ROOT / "backend/app/static/fast-dumping.html").read_text(encoding="utf-8")
    javascript = (ROOT / "backend/app/static/fast-dumping.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    ordinary = (ROOT / "backend/app/dumping_models.py").read_text(encoding="utf-8")

    assert "/crm/fast-dumping" in html
    assert "Товары, остановленные floor" in html
    assert "Изменить порог" in javascript
    assert "/api/fast-dumping" in javascript
    assert 'value="600"' in html and 'value="300"' in html
    assert all(f'value="{seconds}"' in html for seconds in (900, 1800, 2100, 3600))
    assert "10 минут (рекомендуется)" in html
    assert 'id="city-id" maxlength="32" value="196220100"' in html
    assert 'id="delivery-premium"' in html
    assert 'id="delivery-days"' in html
    assert "delivery_price_premium_kzt" in javascript
    assert "delivery_advantage_days" in javascript
    assert "decision_reason" in javascript
    assert 'document.querySelector("#city-id").value = ordinary.policy.city_id' not in javascript
    assert "password_dpapi" in agent and "mc_sid_dpapi" in agent
    assert "fast_dumping" not in ordinary
