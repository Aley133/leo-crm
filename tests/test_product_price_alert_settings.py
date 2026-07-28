from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app import product_detail_api
from backend.app.models import Product
from backend.app.product_detail_api import (
    ProductPriceAlertUpdate,
    test_product_price_alert as send_test_product_price_alert,
    update_product_price_alert,
)


def _product(session: Session) -> Product:
    product = Product(
        kaspi_product_id="ALERT-SETTINGS-001",
        merchant_sku="SKU-ALERT-001",
        name="Карточка для Telegram",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def test_product_price_alert_is_disabled_by_default_and_can_be_enabled(
    db_session: Session,
) -> None:
    product = _product(db_session)
    assert product.sudden_price_alert_enabled is False

    result = update_product_price_alert(
        product.id,
        ProductPriceAlertUpdate(enabled=True),
        db_session,
    )

    assert result.enabled is True
    db_session.refresh(product)
    assert product.sudden_price_alert_enabled is True


def test_test_notification_reports_missing_render_configuration(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _product(db_session)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(send_test_product_price_alert(product.id, db_session))

    assert exc_info.value.status_code == 503
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value.detail)
    assert "TELEGRAM_CHAT_ID" in str(exc_info.value.detail)


def test_test_notification_sends_the_selected_product_identity(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _product(db_session)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    delivered: dict[str, object] = {}

    async def fake_send(client, **kwargs: object) -> None:
        delivered.update(kwargs)

    monkeypatch.setattr(
        product_detail_api,
        "send_test_price_alert_message",
        fake_send,
    )

    result = asyncio.run(send_test_product_price_alert(product.id, db_session))

    assert result.delivered is True
    assert result.message == "Тестовое уведомление отправлено в Telegram."
    assert delivered == {
        "settings": product_detail_api.TelegramPriceAlertSettings(
            bot_token="secret",
            chat_id="-100123",
        ),
        "product_name": "Карточка для Telegram",
        "merchant_sku": "SKU-ALERT-001",
        "kaspi_product_id": "ALERT-SETTINGS-001",
    }
