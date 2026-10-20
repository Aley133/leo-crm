import asyncio
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app import market_recovery as market
from backend.app.models import Product, OutboxEvent
from backend.app.product_test_models import ProductTestItem
from backend.app.product_test_api import ProductTestAgentIdentity
from backend.app.workspace_context import workspace_context
from backend.app.telegram_price_alerts import TelegramPriceAlertSettings, publish_pending_price_alerts
from tools.product_discovery import market_watch
from tools.kaspi_fast_dumping_scanner import _validated_market_offers


IDENTITY = dict(agent_id="market-agent", workspace_id=1, merchant_uid="merchant", agent_kind="product_test", version="1.1.27")


def seed(db, sku="123456789", workspace=1):
    with workspace_context(workspace):
        db.add(Product(workspace_id=workspace, kaspi_product_id=sku, merchant_sku=sku, name="Спящий товар", sale_enabled=False, status="archived"))
        db.commit()


def claim(db, monkeypatch, **identity):
    monkeypatch.setattr(market, "_validate_workspace_merchant", lambda *a, **kw: None)
    return market.claim_card(ProductTestAgentIdentity(**{**IDENTITY, **identity}), db)["card"]


def complete(db, card, price=None, sellers=3, **kwargs):
    payload = market.MarketResult(**IDENTITY, lease_token=card["lease_token"], status="succeeded", price=price, seller_count=sellers, **kwargs)
    return market.complete_card(card["id"], payload, db)


def due(db, card):
    row = db.get(market.MarketRecoveryCard, card["id"])
    row.next_check_at = market.now() - timedelta(seconds=1)
    db.commit()


def events(db):
    return list(db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == market.EVENT_TYPE)))


def test_sync_includes_disabled_and_old_candidates_without_duplicates_or_cross_workspace(db_session, monkeypatch):
    seed(db_session)
    seed(db_session, "123456789_alias")
    seed(db_session, "222222222", workspace=2)
    db_session.add(ProductTestItem(workspace_id=1, input_reference="old",kaspi_product_id="987654321",merchant_sku="old", name="Старый тест",active=False,added_at=market.now(),kaspi_url="https://kaspi.kz/shop/p/-987654321/"))
    db_session.commit()
    settings = market.settings_for(db_session, 1)
    market.sync_catalog(db_session, settings)
    market.sync_catalog(db_session, settings, force=True)
    cards = list(db_session.scalars(select(market.MarketRecoveryCard)))
    assert {c.kaspi_product_id for c in cards} == {"123456789", "987654321"}
    assert all(c.workspace_id == 1 for c in cards)
    card = claim(db_session, monkeypatch)
    assert card is not None
    second = claim(db_session, monkeypatch, agent_id="second")
    assert second["id"] != card["id"]
    assert claim(db_session, monkeypatch) is None


def test_baseline_gradual_growth_daily_schedule_and_no_repeat(db_session, monkeypatch):
    seed(db_session)
    card = claim(db_session, monkeypatch)
    complete(db_session, card, "10000")
    assert not events(db_session)
    row = db_session.get(market.MarketRecoveryCard, card["id"])
    assert market.aware(row.next_check_at) > market.now() + timedelta(hours=23)
    assert claim(db_session, monkeypatch) is None
    for price, expected in [("12000",0),("14999",0),("15000",1),("15000",1),("16000",1),("20000",2),("9000",2),("13500",3)]:
        due(db_session, card)
        card=claim(db_session, monkeypatch)
        complete(db_session, card, price)
        assert len(events(db_session)) == expected
    assert events(db_session)[-1].payload_json["rise_percent"] == "50.0"


def test_zero_sellers_confirmed_once_and_recovery_does_not_change_sale_state(db_session, monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,"10000")
    due(db_session,card)
    card=claim(db_session,monkeypatch)
    rejected=complete(db_session,card,sellers=0)
    assert rejected["error"]
    row=db_session.get(market.MarketRecoveryCard,card["id"])
    assert row.seller_count==3 and row.current_price==10000
    assert not events(db_session)
    due(db_session,card)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,sellers=0,empty_confirmed=True)
    assert events(db_session)[0].payload_json["reasons"]==["no_sellers"]
    due(db_session,card)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,sellers=0,empty_confirmed=True)
    assert len(events(db_session))==1
    assert db_session.scalar(select(Product.sale_enabled)) is False


def test_first_empty_card_alert_and_result_retry_is_idempotent(db_session, monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,sellers=0,empty_confirmed=True)
    complete(db_session,card,sellers=0,empty_confirmed=True)
    assert len(events(db_session))==1
    assert events(db_session)[0].payload_json["previous_sellers"] is None


def test_wrong_agent_workspace_and_expired_lease_are_rejected(db_session, monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch)
    for changes,code in [({"agent_id":"other"},409),({"workspace_id":2},404),({"version":"1.1.26"},409),({"agent_kind":"browser_agent"},409)]:
        with pytest.raises(HTTPException) as exc:
            market.complete_card(card["id"],market.MarketResult(**{**IDENTITY,**changes},lease_token=card["lease_token"],status="succeeded",price="10000",seller_count=3),db_session)
        assert exc.value.status_code==code
    row=db_session.get(market.MarketRecoveryCard,card["id"])
    row.lease_until=market.now()-timedelta(seconds=1)
    db_session.commit()
    with pytest.raises(HTTPException): complete(db_session,card,"10000")
    new=claim(db_session,monkeypatch)
    assert new["lease_token"]!=card["lease_token"]


def test_any_growth_and_fewer_sellers_without_price_growth(db_session,monkeypatch):
    seed(db_session)
    settings=market.settings_for(db_session,1)
    settings.rise_percent=0
    db_session.commit()
    card=claim(db_session,monkeypatch)
    complete(db_session,card,"10000",sellers=5)
    for price,sellers,expected in [("10000",2,0),("9999",1,0),("10000",1,1),("10000",1,1)]:
        due(db_session,card);card=claim(db_session,monkeypatch);complete(db_session,card,price,sellers=sellers)
        assert len(events(db_session))==expected


def test_telegram_uses_existing_sender_and_suppresses_changed_or_paused_opportunity(db_session,monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,"10000")
    due(db_session,card);card=claim(db_session,monkeypatch);complete(db_session,card,"15000")
    factory=sessionmaker(bind=db_session.get_bind(),expire_on_commit=False)
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"ok":True})
    async def publish():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_pending_price_alerts(settings=TelegramPriceAlertSettings(bot_token="existing-ozon-token",chat_id="existing-chat"),session_factory=factory,client=client)
    assert asyncio.run(publish())==(1,0)
    assert str(requests[0].url)=="https://api.telegram.org/botexisting-ozon-token/sendMessage"
    payload=json.loads(requests[0].content)
    assert payload["chat_id"]=="existing-chat" and "Kaspi" in payload["text"] and "50.0" in payload["text"]
    assert asyncio.run(publish())==(0,0)
    due(db_session,card);card=claim(db_session,monkeypatch);complete(db_session,card,"20000")
    row=db_session.get(market.MarketRecoveryCard,card["id"])
    row.current_price=Decimal("10000");db_session.commit()
    assert asyncio.run(publish())==(0,0)
    assert len(requests)==1


@pytest.mark.parametrize("payload", [{}, {"error":"blocked"}, {"data":{"message":"retry"}}, {"offers":[{}]}, {"offers":[],"error":"blocked"}, {"success":False,"offers":[]}, {"errorCode":"RETRY", "offers":[]}])
def test_invalid_response_is_not_zero_sellers(payload):
    with pytest.raises(ValueError): _validated_market_offers(payload)


def test_empty_seller_schema_is_accepted_and_confirmed_twice(monkeypatch):
    assert _validated_market_offers({"offers":[]})==[]
    calls=[]
    async def inspect(**kwargs):
        calls.append(kwargs)
        assert kwargs["strict_offers"] is True
        return {"offers":{"seller_count_scanned":0,"best_offer_price_kzt":None,"complete":True}}
    async def sleep(_):pass
    monkeypatch.setattr(market_watch,"inspect_kaspi_product",inspect)
    monkeypatch.setattr(market_watch.asyncio,"sleep",sleep)
    result=asyncio.run(market_watch.inspect_market_card({"reference":"123456789","city_id":"196220100","zone_id":"Magnum_ZONE1"}))
    assert result["empty_confirmed"] is True and len(calls)==2


def test_error_and_inconsistent_empty_confirmation_do_not_emit_result(monkeypatch):
    rows=iter([0,2])
    async def inspect(**kwargs):
        n=next(rows)
        return {"offers":{"seller_count_scanned":n,"best_offer_price_kzt":"10000" if n else None,"complete":True}}
    async def sleep(_):pass
    monkeypatch.setattr(market_watch,"inspect_kaspi_product",inspect)
    monkeypatch.setattr(market_watch.asyncio,"sleep",sleep)
    with pytest.raises(ValueError):
        asyncio.run(market_watch.inspect_market_card({"reference":"123456789","city_id":"196220100","zone_id":"Magnum_ZONE1"}))


def test_pause_and_old_version_cannot_claim_and_catalog_refresh_keeps_history(db_session,monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch)
    complete(db_session,card,"10000")
    market.update_settings(market.SettingsUpdate(enabled=False,rise_percent=100),db_session)
    due(db_session,card)
    assert claim(db_session,monkeypatch) is None
    with pytest.raises(HTTPException): claim(db_session,monkeypatch,version="1.1.26")
    seed(db_session,"333333333")
    market.sync_assortment(db_session)
    assert db_session.get(market.MarketRecoveryCard,card["id"]).baseline_price==10000
    assert db_session.scalar(select(market.MarketRecoveryCard.id).where(market.MarketRecoveryCard.kaspi_product_id=="333333333"))


def test_price_recovery_after_empty_market_uses_preserved_baseline(db_session,monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch);complete(db_session,card,"10000")
    due(db_session,card);card=claim(db_session,monkeypatch);complete(db_session,card,sellers=0,empty_confirmed=True)
    due(db_session,card);card=claim(db_session,monkeypatch);complete(db_session,card,"15000")
    assert events(db_session)[-1].payload_json["reasons"]==["price_rise"]


def test_draft_search_candidates_are_not_our_assortment(db_session):
    db_session.add(ProductTestItem(workspace_id=1,input_reference="search",kaspi_product_id="111111111",merchant_sku="search",name="Найдено в поиске",kaspi_url="https://kaspi.kz/shop/p/-111111111/"))
    db_session.commit()
    settings=market.settings_for(db_session,1)
    market.sync_catalog(db_session,settings)
    assert list(db_session.scalars(select(market.MarketRecoveryCard)))==[]


def test_failed_scan_retains_baseline_and_retries_in_one_hour(db_session,monkeypatch):
    seed(db_session)
    card=claim(db_session,monkeypatch);complete(db_session,card,"10000")
    due(db_session,card);card=claim(db_session,monkeypatch)
    market.complete_card(card["id"],market.MarketResult(**IDENTITY,lease_token=card["lease_token"],status="failed",error="HTTP 403"),db_session)
    row=db_session.get(market.MarketRecoveryCard,card["id"])
    assert row.current_price==10000 and row.seller_count==3 and row.baseline_price==10000
    assert row.last_error=="HTTP 403"
    assert timedelta(minutes=59)<market.aware(row.next_check_at)-market.now()<timedelta(hours=1,seconds=1)
    assert not events(db_session)
