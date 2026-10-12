from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import OutboxEvent, Product
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct
from backend.app.price_drop_alerts import PRICE_DROP_EVENT_TYPE
from backend.app.telegram_price_alerts import (
    PRICE_ALERT_RUNTIME_STATUS,
    TelegramDeliveryError,
    TelegramPriceAlertSettings,
    format_price_drop_message,
    price_alert_publisher_loop,
    price_alert_runtime_status,
    publish_pending_price_alerts,
    send_price_drop_message,
    _confirmed_offer,
    _pending_events,
)


def _payload() -> dict[str, object]:
    return {
        "version": 3,
        "price_checked_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
        "supplier_source_url": "https://www.ozon.ru/product/123/?from=crm&price=low",
        "supplier_seller": "CSCPharma",
        "delivery_days": 6,
        "supplier_product_id": 7,
        "observation_id": 42,
        "baseline_price": "3050.00",
        "current_price": "1000.00",
        "drop_percent": "67.2",
        "threshold_percent": 50,
        "currency": "KZT",
        "baseline_sample_size": 6,
        "observed_at": datetime(2026, 7, 29, 10, 6, tzinfo=UTC).isoformat(),
        "product_id": 3,
        "product_name": "Берберин <500 мг>",
        "merchant_sku": "BERB-60",
        "kaspi_product_id": "101010101",
        "supplier_code": "ozon",
        "supplier_name": "Ozon",
        "supplier_product_title": "Берберин & хром",
        "supplier_product_url": "https://www.ozon.ru/product/123/?from=crm&price=low",
        "binding_id": 9,
    }


def _seed_live_offer(session):
    now = datetime.now(UTC)
    supplier = Supplier(code="ozon", name="Ozon")
    session.add(supplier)
    session.flush()
    source = SupplierProduct(id=7, supplier_id=supplier.id, external_id="123", title="Берберин", url=_payload()["supplier_source_url"])
    session.add(source)
    session.flush()
    binding = ProductBinding(id=9, product_id=3, supplier_product_id=7, status="active")
    session.add(binding)
    session.flush()
    target = MonitorTarget(product_binding_id=9, status="active", interval_seconds=300, next_check_at=now)
    state = SupplierOfferState(supplier_product_id=7, price=1000, currency="KZT", available=True, seller="CSCPharma", delivery_days=6, fingerprint="a" * 64, adapter_schema_version="ozon-http-session-v1", observed_at=now - timedelta(minutes=6), last_checked_at=now - timedelta(seconds=10))
    session.add_all([target, state])
    session.flush()
    return binding, target, source, state


def test_message_is_actionable_and_html_safe() -> None:
    message = format_price_drop_message(_payload())

    assert "Резко упала закупочная цена" in message
    assert "Обычная цена: <s>3 050 ₸</s>" in message
    assert "Сейчас: <b>1 000 ₸</b>" in message
    assert "−67.2%" in message
    assert "Порог сигнала: 50%" in message
    assert "подтверждена повторной проверкой" in message
    assert "Продавец: CSCPharma" in message
    assert "Доставка: 6 дн." in message
    assert "Берберин &lt;500 мг&gt;" in message
    assert "from=crm&amp;price=low" in message


def test_environment_requires_both_telegram_values(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramPriceAlertSettings.from_environment() is None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    assert TelegramPriceAlertSettings.from_environment() is None

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    settings = TelegramPriceAlertSettings.from_environment()
    assert settings is not None
    assert settings.bot_token == "secret"
    assert settings.chat_id == "-100123"


def test_publisher_reports_missing_configuration_instead_of_stopping_silently(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    PRICE_ALERT_RUNTIME_STATUS.update({
        "status": "starting",
        "configured": None,
        "last_cycle_at": None,
        "last_success_at": None,
        "last_error": None,
        "sent_count": 0,
        "failed_count": 0,
    })

    async def run() -> dict[str, object]:
        stop_event = asyncio.Event()
        task = asyncio.create_task(price_alert_publisher_loop(stop_event))
        for _attempt in range(10):
            await asyncio.sleep(0)
            if price_alert_runtime_status()["configured"] is False:
                break
        snapshot = price_alert_runtime_status()
        stop_event.set()
        await task
        return snapshot

    status = asyncio.run(run())
    assert status["status"] == "not_configured"
    assert status["configured"] is False
    assert status["last_error"] == "telegram_credentials_missing"
    assert status["last_cycle_at"] is not None


def test_telegram_rejection_does_not_expose_the_bot_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: chat not found"},
        )

    settings = TelegramPriceAlertSettings(
        bot_token="do-not-leak-this-secret",
        chat_id="-100123",
        api_base_url="https://telegram.example",
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_price_drop_message(
                client,
                settings=settings,
                payload=_payload(),
            )

    try:
        asyncio.run(run())
    except TelegramDeliveryError as exc:
        assert "chat not found" in str(exc)
        assert settings.bot_token not in str(exc)
    else:
        raise AssertionError("TelegramDeliveryError was not raised")


def test_outbox_event_is_marked_only_after_telegram_accepts_it() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_id = uuid4()
    with factory() as session:
        session.add(
            Product(
                id=3,
                kaspi_product_id="101010101",
                merchant_sku="BERB-60",
                name="Берберин 500 мг",
                sudden_price_alert_enabled=True,
            )
        )
        _seed_live_offer(session)
        session.add(
            OutboxEvent(
                id=event_id,
                aggregate_type="supplier_product",
                aggregate_id="7",
                event_type=PRICE_DROP_EVENT_TYPE,
                idempotency_key="supplier-price-drop:7:observation:42",
                payload_json=_payload(),
            )
        )
        session.commit()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    settings = TelegramPriceAlertSettings(
        bot_token="secret",
        chat_id="-100123",
        api_base_url="https://telegram.example",
    )
    async def run() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_pending_price_alerts(
                settings=settings,
                session_factory=factory,
                client=client,
            )

    assert asyncio.run(run()) == (1, 0)
    assert len(requests) == 1
    assert requests[0].url == "https://telegram.example/botsecret/sendMessage"

    with factory() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id))
        assert event is not None
        assert event.published_at is not None
        assert event.publish_attempts == 1
        assert event.last_error is None

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_pending_event_is_suppressed_if_product_was_disabled() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_id = uuid4()
    with factory() as session:
        session.add(
            Product(
                id=3,
                kaspi_product_id="101010101",
                merchant_sku="BERB-60",
                name="Берберин 500 мг",
                sudden_price_alert_enabled=False,
            )
        )
        session.add(
            OutboxEvent(
                id=event_id,
                aggregate_type="supplier_product",
                aggregate_id="7",
                event_type=PRICE_DROP_EVENT_TYPE,
                idempotency_key="supplier-price-drop:7:observation:disabled",
                payload_json=_payload(),
            )
        )
        session.commit()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = TelegramPriceAlertSettings(
        bot_token="secret",
        chat_id="-100123",
        api_base_url="https://telegram.example",
    )

    async def run() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_pending_price_alerts(
                settings=settings,
                session_factory=factory,
                client=client,
            )

    assert asyncio.run(run()) == (0, 0)
    assert requests == []

    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.published_at is not None
        assert event.publish_attempts == 0
        assert event.last_error == "suppressed: product price alert disabled"

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_pending_event_respects_a_threshold_changed_before_delivery() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_id = uuid4()
    payload = _payload()
    payload["drop_percent"] = "20.0"
    payload["threshold_percent"] = 10
    with factory() as session:
        session.add(
            Product(
                id=3,
                kaspi_product_id="101010101",
                merchant_sku="BERB-60",
                name="Берберин 500 мг",
                sudden_price_alert_enabled=True,
                sudden_price_alert_threshold_percent=50,
            )
        )
        session.add(
            OutboxEvent(
                id=event_id,
                aggregate_type="supplier_product",
                aggregate_id="7",
                event_type=PRICE_DROP_EVENT_TYPE,
                idempotency_key="supplier-price-drop:7:observation:threshold-changed",
                payload_json=payload,
            )
        )
        session.commit()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = TelegramPriceAlertSettings(
        bot_token="secret",
        chat_id="-100123",
        api_base_url="https://telegram.example",
    )

    async def run() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_pending_price_alerts(
                settings=settings,
                session_factory=factory,
                client=client,
            )

    assert asyncio.run(run()) == (0, 0)
    assert requests == []
    with factory() as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.published_at is not None
        assert event.last_error == "suppressed: product price alert threshold changed"

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_confirmation_waits_for_an_independent_check_even_without_new_observation(db_session):
    _, _, _, state = _seed_live_offer(db_session)
    payload = _payload()
    now = datetime.now(UTC)
    state.last_checked_at = datetime.fromisoformat(payload['price_checked_at'])
    db_session.flush()
    assert _confirmed_offer(db_session, payload, now=now) == ('waiting', None)
    # An unchanged fingerprint produces no observation; its successful next
    # check alone is sufficient proof that the price survived a second read.
    state.last_checked_at = now - timedelta(seconds=1)
    db_session.flush()
    assert _confirmed_offer(db_session, payload, now=now) == ('ready', None)
    assert payload['confirmed_at'] == state.last_checked_at.isoformat()


def test_confirmation_rejects_a_price_that_recovered_before_delivery(db_session):
    _, _, _, state = _seed_live_offer(db_session)
    state.price = 3050
    db_session.flush()
    assert _confirmed_offer(db_session, _payload(), now=datetime.now(UTC)) == (
        'suppressed', 'price or seller changed before confirmation',
    )


def test_confirmation_rejects_a_changed_seller_even_at_the_same_price(db_session):
    _, _, _, state = _seed_live_offer(db_session)
    state.seller = 'Другой продавец'
    db_session.flush()
    assert _confirmed_offer(db_session, _payload(), now=datetime.now(UTC))[0] == 'suppressed'


def test_confirmation_rejects_removed_or_replaced_sources(db_session):
    binding, target, source, _ = _seed_live_offer(db_session)
    binding.status = 'disabled'
    db_session.flush()
    assert _confirmed_offer(db_session, _payload(), now=datetime.now(UTC))[0] == 'suppressed'
    binding.status = 'active'
    source.url = 'https://www.ozon.kz/product/456/'
    db_session.flush()
    assert _confirmed_offer(db_session, _payload(), now=datetime.now(UTC)) == (
        'suppressed', 'supplier link changed',
    )


def test_confirmation_rejects_expired_and_unverified_legacy_alerts(db_session):
    payload = _payload()
    payload['price_checked_at'] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert _confirmed_offer(db_session, payload, now=datetime.now(UTC)) == ('suppressed', 'price alert expired')
    payload['version'] = 2
    assert _confirmed_offer(db_session, payload, now=datetime.now(UTC)) == ('suppressed', 'unverified legacy price alert')


def test_publisher_discards_recovered_price_instead_of_sending_it(db_session):
    product = Product(id=3, kaspi_product_id='TEST-DROP', name='Товар', sudden_price_alert_enabled=True)
    db_session.add(product)
    _, _, _, state = _seed_live_offer(db_session)
    state.price = 3050
    event = OutboxEvent(aggregate_type='supplier_product', aggregate_id='7', event_type=PRICE_DROP_EVENT_TYPE, idempotency_key='recovered-drop', payload_json=_payload())
    db_session.add(event)
    db_session.commit()
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    assert _pending_events(factory, limit=10) == []
    db_session.refresh(event)
    assert event.published_at is not None
    assert event.publish_attempts == 0
    assert event.last_error == 'suppressed: price or seller changed before confirmation'
