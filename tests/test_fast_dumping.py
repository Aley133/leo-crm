from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, select

from backend.app.dumping_service import calculate_safe_floor
from backend.app.fast_dumping_agent_api import _validate_workspace_merchant
from backend.app.fast_dumping_api import (
    list_fast_dumping_products,
    read_fast_dumping_offers,
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
    ensure_state,
    prepare_apply,
    queue_scan,
)
from backend.app.inventory_models import InventoryBatch
from backend.app.models import MarketplaceAccount, Product
from backend.app.workspace_context import workspace_context
from backend.app.workspace_models import KaspiAccountCredential, Workspace
from tools.kaspi_fast_dumping_scanner import _merchant_id, _own_match, _page_visible_price


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
            scan_interval_seconds=10,
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


def test_unconfirmed_write_latches_only_fast_product(db_session) -> None:
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
        assert prepare_apply(
            db_session,
            workspace_id=1,
            job_id=job.id,
            agent_id="fast-agent",
            lease_token=leased_apply.lease_token,
        )["ready"]
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
                "latency_seconds": 120,
            },
        )
        db_session.commit()
    db_session.refresh(state)
    assert state.status == "apply_timeout"
    assert state.automatic_writes_paused is True
    assert state.next_scan_at is None


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


def test_fast_dumping_ui_and_agent_are_separate_from_ordinary_dumping() -> None:
    html = (ROOT / "backend/app/static/fast-dumping.html").read_text(encoding="utf-8")
    javascript = (ROOT / "backend/app/static/fast-dumping.js").read_text(encoding="utf-8")
    agent = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    ordinary = (ROOT / "backend/app/dumping_models.py").read_text(encoding="utf-8")

    assert "/crm/fast-dumping" in html
    assert "Товары, остановленные floor" in html
    assert "Изменить порог" in javascript
    assert "/api/fast-dumping" in javascript
    assert "password_dpapi" in agent and "mc_sid_dpapi" in agent
    assert "fast_dumping" not in ordinary
