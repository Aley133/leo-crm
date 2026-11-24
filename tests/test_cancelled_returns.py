from datetime import UTC, datetime, timedelta
from decimal import Decimal
import asyncio

import httpx
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from backend.app.cancelled_returns import (
    HANDOFF, TRACKED, RECEIVED, COMPENSATED, ALERT, TRACKING_START, WAIT, add_event, current_alert,
    find_event, handoff_time, observe_return, scan_returns,
)
from backend.app.inventory_models import InventoryBatch
from backend.app.inventory_service import allocate_order_line_fifo, create_inventory_batch, rebuild_product_fifo
from backend.app.kaspi_order_payload import canonicalize_kaspi_order_payload
from backend.app.marketplace_import import import_kaspi_order
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderEvent, OutboxEvent, Product
from backend.app.telegram_price_alerts import TelegramPriceAlertSettings, publish_pending_price_alerts

NOW = datetime(2026, 11, 25, 10, tzinfo=UTC)
CANCELLED = NOW - timedelta(days=10)
HANDOVER = CANCELLED - timedelta(days=2)


def account(session):
    acc = MarketplaceAccount(provider="kaspi", external_account_id="bar", display_name="BARWORK")
    session.add(acc)
    session.flush()
    return acc


def payload(status, *, courier=None):
    attrs = {"status": status, "code": "123", "totalPrice": 7000,
             "updatedAt": CANCELLED.isoformat(), "entries": [{"id":"l1", "name":"Товар", "quantity":2,"basePrice":3500}]}
    if courier:
        attrs["courierTransmissionDate"] = courier.timestamp() * 1000
    return {"id": "order1", "attributes": attrs}


def import_order(session, acc, status="CANCELLED", courier=HANDOVER):
    result = import_kaspi_order(session, marketplace_account_id=acc.id,
                               payload=payload(status, courier=courier))
    session.flush()
    return session.get(MarketplaceOrder, result.order_id)


@pytest.mark.parametrize("attrs", [
    {"status":"CANCELLED"},
    {"status":"SHIPPING", "marketplaceStatus":"ACCEPTED_BY_MERCHANT", "leoClassificationSource":"planned_transmission_date"},
    {"status":"SHIPPING", "leoClassificationSource":"first_import_old_order"},
    {"courierTransmissionPlanningDate":HANDOVER.timestamp()*1000, "deliveryCostForSeller":1000},
    {"kaspiDelivery": {"courierTransmissionDate": None,
                       "courierTransmissionPlanningDate": HANDOVER.timestamp() * 1000}},
    {"kaspiDelivery": {"courierTransmissionDate": True}},
    {"courierTransmissionDate":(NOW+timedelta(days=1)).timestamp()*1000},
    {"courierTransmissionDate":"garbage"},
])
def test_estimates_and_invalid_dates_do_not_prove_dispatch(attrs):
    assert handoff_time({"attributes":attrs},NOW) is None


def test_cancelled_after_actual_dispatch_is_tracked_once(db_session):
    acc = account(db_session)
    order = import_order(db_session,acc)
    assert find_event(order,HANDOFF).occurred_at == HANDOVER
    assert find_event(order,TRACKED).occurred_at == CANCELLED
    import_order(db_session,acc)
    assert len([e for e in order.events if e.event_type == TRACKED]) == 1


@pytest.mark.parametrize("status", ["CANCELLING", "CANCELLED"])
def test_cancellation_before_dispatch_excluded(db_session, status):
    order = import_order(db_session, account(db_session), status, courier=None)
    scan_returns(db_session,now=NOW)
    assert find_event(order,TRACKED) is None
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 0


def test_evidence_survives_cancel_payload_without_courier_date(db_session):
    acc = account(db_session)
    order = import_order(db_session,acc,status="SHIPPING")
    import_order(db_session,acc,courier=None)
    assert find_event(order,TRACKED)


def test_ten_day_threshold_and_durable_deduplication(db_session):
    import_order(db_session,account(db_session))
    scan_returns(db_session,now=NOW-timedelta(seconds=1))
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 0
    scan_returns(db_session,now=NOW)
    scan_returns(db_session,now=NOW+timedelta(days=1))
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 1


def test_alert_cutoff_keeps_overdue_history_visible_without_sending(db_session):
    order = import_order(db_session, account(db_session), status="CANCELLING")
    after_due = NOW + timedelta(seconds=1)
    scan_returns(db_session, now=after_due, alerts_start_at=after_due)
    assert find_event(order, TRACKED)
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 0

    scan_returns(db_session, now=after_due, alerts_start_at=NOW)
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 1


def test_november_cutoff_is_inclusive_and_scan_skips_old_and_already_queued(db_session, monkeypatch):
    from fastapi import HTTPException
    from backend.app import cancelled_returns_api as api

    acc = account(db_session)
    orders = []
    assert TRACKING_START == datetime(2026, 11, 14, 19, tzinfo=UTC)
    for code, when in (("old", TRACKING_START - timedelta(milliseconds=1)), ("new", TRACKING_START)):
        raw = payload("CANCELLED", courier=HANDOVER)
        raw["id"] = code
        raw["attributes"].update(code=code, updatedAt=when.isoformat())
        result = import_kaspi_order(db_session, marketplace_account_id=acc.id, payload=raw)
        orders.append(db_session.get(MarketplaceOrder, result.order_id))

    old, new = orders
    assert find_event(old, TRACKED)
    assert not current_alert(db_session, {"order_id": old.id})
    due = TRACKING_START + WAIT
    assert scan_returns(db_session, limit=1, now=due - timedelta(milliseconds=1)) == 0
    assert scan_returns(db_session, limit=1, now=due) == new.id
    assert scan_returns(db_session, limit=1, now=due) == 0
    assert db_session.scalar(select(OutboxEvent.aggregate_id)) == str(new.id)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    for view in ("pending", "all"):
        listing = api.list_returns(view=view, query=None, account_id=None, limit=50, offset=0)
        assert listing["total"] == 1
        assert listing["total_amount"] == "7000.00"
        assert [item["id"] for item in listing["items"]] == [new.id]
    with pytest.raises(HTTPException) as conflict:
        api.receive_return(old.id)
    assert conflict.value.status_code == 409


@pytest.mark.parametrize("status", ["CANCELLING", "CANCELLED"])
def test_backfill_rechecks_legacy_orders_for_nested_dispatch_evidence(db_session, status):
    acc = account(db_session)
    raw = payload(status)
    raw["attributes"]["kaspiDelivery"] = {"courierTransmissionDate": int(HANDOVER.timestamp() * 1000)}
    raw["attributes"]["cancellationReason"] = "TIMEOUT_BUYER_PICKUP"
    canonical = canonicalize_kaspi_order_payload(raw, now=NOW)
    result = import_kaspi_order(db_session, marketplace_account_id=acc.id, payload=canonical)
    order = db_session.get(MarketplaceOrder, result.order_id)
    assert find_event(order, HANDOFF).occurred_at == HANDOVER
    assert find_event(order, TRACKED).occurred_at == CANCELLED

    for event in list(order.events):
        if event.event_type in {HANDOFF,TRACKED}:
            order.events.remove(event)
    add_event(order, "cancelled_return_evidence_checked", NOW)
    db_session.flush()
    scan_returns(db_session,now=NOW)
    assert find_event(order, HANDOFF).occurred_at == HANDOVER
    assert find_event(order, TRACKED).occurred_at == CANCELLED

    final = payload("CANCELLED")
    final["attributes"]["updatedAt"] = NOW.isoformat()
    import_kaspi_order(db_session, marketplace_account_id=acc.id, payload=final)
    assert len([event for event in order.events if event.event_type == TRACKED]) == 1
    assert find_event(order, TRACKED).occurred_at == CANCELLED


@pytest.mark.parametrize("status", ["CANCELLING", "CANCELLED"])
def test_receipt_is_idempotent_and_hides_pending_alert(db_session,monkeypatch,status):
    from backend.app import cancelled_returns_api as api
    order = import_order(db_session,account(db_session),status)
    scan_returns(db_session,now=NOW)
    db_session.commit()
    factory=sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    monkeypatch.setattr(api,"SessionLocal",factory)
    listing = api.list_returns(view="pending", query=None, account_id=None, limit=50, offset=0)
    assert listing["total"] == 1
    assert listing["items"][0]["can_receive"]
    result=api.receive_return(order.id)
    assert result["already_received"] is False
    assert api.receive_return(order.id)["already_received"] is True
    with factory() as session:
        assert session.scalar(select(func.count(MarketplaceOrderEvent.id)).where(MarketplaceOrderEvent.event_type==RECEIVED))==1
    sent=[]
    async def send(request):
        sent.append(request)
        return httpx.Response(200,json={"ok":True})
    async def publish():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            return await publish_pending_price_alerts(settings=TelegramPriceAlertSettings("test-token","123"),session_factory=factory,client=client)
    assert asyncio.run(publish()) == (0,0)
    assert sent == []
    listing=api.list_returns(view="received",query=None,account_id=None,limit=50,offset=0)
    assert listing["total"] == 1
    assert listing["items"][0]["state"] == "received"
    assert api.list_returns(view="pending",query=None,account_id=None,limit=50,offset=0)["total"]==0


def test_telegram_failure_retries_and_success_does_not_repeat(db_session):
    import_order(db_session,account(db_session))
    scan_returns(db_session,now=NOW)
    db_session.commit()
    factory=sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    calls=[]
    async def send(request):
        calls.append(request)
        return httpx.Response(500 if len(calls)==1 else 200,json={"ok":len(calls)>1})
    async def publish():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            settings=TelegramPriceAlertSettings("test-token","123")
            first=await publish_pending_price_alerts(settings=settings,session_factory=factory,client=client)
            second=await publish_pending_price_alerts(settings=settings,session_factory=factory,client=client)
            third=await publish_pending_price_alerts(settings=settings,session_factory=factory,client=client)
            return first,second,third
    assert asyncio.run(publish()) == ((0,1),(1,0),(0,0))
    assert len(calls)==2


@pytest.mark.parametrize("status", ["CANCELLING", "CANCELLED"])
def test_compensation_closes_return_without_restocking(db_session, monkeypatch, status):
    from fastapi import HTTPException
    from backend.app import cancelled_returns_api as api

    acc = account(db_session)
    order = import_order(db_session, acc, status="SHIPPING")
    product = Product(kaspi_product_id="105721344", merchant_sku="105721344", name="Товар", status="active")
    db_session.add(product)
    db_session.flush()
    batch, _ = create_inventory_batch(
        db_session, product=product, quantity=3, unit_cost=Decimal("500"),
        received_at=HANDOVER - timedelta(days=1), reconcile_existing_orders=False,
    )
    order.lines[0].product_id = product.id
    allocate_order_line_fifo(db_session, order_line=order.lines[0], order=order)
    assert batch.quantity_remaining == 1
    import_order(db_session, acc, status=status)
    scan_returns(db_session, now=NOW)
    db_session.commit()

    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    first = api.compensate_return(order.id)
    assert not first["already_compensated"]
    assert api.compensate_return(order.id) == {**first, "already_compensated": True}
    with pytest.raises(HTTPException) as conflict:
        api.receive_return(order.id)
    assert conflict.value.status_code == 409

    for view in ("pending", "overdue", "received", "compensated", "all"):
        listing = api.list_returns(view=view, query=None, account_id=None, limit=50, offset=0)
        if view in {"compensated", "all"}:
            item, = listing["items"]
            assert item["state"] == "compensated"
            assert item["compensated_at"] == first["compensated_at"]
            assert not item["can_receive"]
        else:
            assert listing["total"] == 0

    with factory() as session:
        alert = session.scalar(select(OutboxEvent).where(OutboxEvent.event_type == ALERT))
        assert alert is not None
        assert not current_alert(session, alert.payload_json)
        assert session.get(InventoryBatch, batch.id).quantity_remaining == 1
        rebuild_product_fifo(session, product_id=product.id)
        assert session.get(InventoryBatch, batch.id).quantity_remaining == 1
        assert session.scalar(select(func.count(MarketplaceOrderEvent.id)).where(
            MarketplaceOrderEvent.event_type == COMPENSATED,
        )) == 1


def test_api_respects_workspace_for_read_and_receive(db_session, monkeypatch):
    from backend.app import legacy_workspace_scope  # noqa: F401
    from backend.app import cancelled_returns_api as api
    from backend.app.workspace_context import set_current_workspace_id, reset_current_workspace_id
    from fastapi import HTTPException
    order = import_order(db_session, account(db_session))
    db_session.commit()
    factory = sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    monkeypatch.setattr(api,"SessionLocal",factory)
    token = set_current_workspace_id(999)
    try:
        assert api.list_returns(view="all",query=None,account_id=None,limit=50,offset=0)["total"] == 0
        with pytest.raises(HTTPException) as error:
            api.receive_return(order.id)
        assert error.value.status_code == 404
    finally:
        reset_current_workspace_id(token)


def test_unshipped_history_checked_once_but_later_evidence_reactivates(db_session):
    from backend.app.cancelled_returns import CHECKED
    acc = account(db_session)
    order = import_order(db_session, acc, courier=None)
    scan_returns(db_session, now=NOW)
    assert find_event(order, CHECKED)
    assert not find_event(order, TRACKED)
    import_order(db_session, acc, courier=HANDOVER)
    assert find_event(order, TRACKED)
    scan_returns(db_session, now=NOW)
    assert db_session.scalar(select(func.count(OutboxEvent.id))) == 1
