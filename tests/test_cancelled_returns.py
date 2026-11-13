from datetime import UTC, datetime, timedelta
import asyncio

import httpx
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from backend.app.cancelled_returns import (
    HANDOFF, TRACKED, RECEIVED, ALERT, WAIT, add_event, find_event,
    handoff_time, observe_return, scan_returns,
)
from backend.app.marketplace_import import import_kaspi_order
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderEvent, OutboxEvent
from backend.app.telegram_price_alerts import TelegramPriceAlertSettings, publish_pending_price_alerts

NOW = datetime(2026, 11, 13, 10, tzinfo=UTC)
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


def test_cancellation_before_dispatch_excluded(db_session):
    order = import_order(db_session,account(db_session),courier=None)
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


def test_backfill_recovers_raw_evidence(db_session):
    order = import_order(db_session,account(db_session))
    for event in list(order.events):
        if event.event_type in {HANDOFF,TRACKED}:
            order.events.remove(event)
    db_session.flush()
    scan_returns(db_session,now=NOW)
    assert find_event(order,TRACKED)


def test_receipt_is_idempotent_and_hides_pending_alert(db_session,monkeypatch):
    from backend.app import cancelled_returns_api as api
    order = import_order(db_session,account(db_session))
    scan_returns(db_session,now=NOW)
    db_session.commit()
    factory=sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    monkeypatch.setattr(api,"SessionLocal",factory)
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
