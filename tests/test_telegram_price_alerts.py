from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import OutboxEvent, Product
from backend.app.price_drop_alerts import PRICE_DROP_EVENT_TYPE
from backend.app.telegram_price_alerts import (
    TelegramDeliveryError,
    TelegramPriceAlertSettings,
    format_price_drop_message,
    format_test_price_alert_message,
    publish_pending_price_alerts,
    send_test_price_alert_message,
)


def _payload() -> dict[str, object]:
    return {
        "version": 1,
        "supplier_product_id": 7,
        "observation_id": 42,
        "baseline_price": "3050.00",
        "current_price": "1000.00",
        "drop_percent": "67.2",
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


def test_message_is_actionable_and_html_safe() -> None:
    message = format_price_drop_message(_payload())

    assert "Резко упала закупочная цена" in message
    assert "Обычная цена: <s>3 050 ₸</s>" in message
    assert "Сейчас: <b>1 000 ₸</b>" in message
    assert "−67.2%" in message
    assert "даже без текущего заказа" in message
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


def test_test_message_identifies_the_product_and_is_html_safe() -> None:
    message = format_test_price_alert_message(
        product_name="Берберин <500 мг>",
        merchant_sku="BERB&60",
        kaspi_product_id="101010101",
    )

    assert "Тестовое уведомление LEO CRM" in message
    assert "Telegram подключён правильно" in message
    assert "Берберин &lt;500 мг&gt;" in message
    assert "SKU: BERB&amp;60" in message
    assert "Kaspi ID: 101010101" in message


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
            await send_test_price_alert_message(
                client,
                settings=settings,
                product_name="Товар",
                merchant_sku=None,
                kaspi_product_id="123",
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
