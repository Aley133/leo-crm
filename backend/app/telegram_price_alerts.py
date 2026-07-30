from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from typing import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import OutboxEvent, Product
from .price_drop_alerts import PRICE_DROP_EVENT_TYPE


PUBLISH_INTERVAL_SECONDS = 10
PUBLISH_BATCH_SIZE = 10
SessionFactory = Callable[[], Session]


class TelegramDeliveryError(RuntimeError):
    """A sanitized Telegram failure that never exposes the bot token."""


@dataclass(frozen=True, slots=True)
class TelegramPriceAlertSettings:
    bot_token: str
    chat_id: str
    api_base_url: str = "https://api.telegram.org"

    @classmethod
    def from_environment(cls) -> TelegramPriceAlertSettings | None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not bot_token or not chat_id:
            return None
        api_base_url = (
            os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org")
            .strip()
            .rstrip("/")
        )
        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            api_base_url=api_base_url or "https://api.telegram.org",
        )


def _money(value: object, currency: object) -> str:
    amount = Decimal(str(value))
    rendered = f"{amount:,.0f}".replace(",", " ")
    code = str(currency or "KZT").upper()
    symbol = {"KZT": "₸", "RUB": "₽"}.get(code, code)
    return f"{rendered} {symbol}"


def format_price_drop_message(payload: dict[str, object]) -> str:
    product_name = escape(str(payload.get("product_name") or "Товар"))
    supplier_name = escape(str(payload.get("supplier_name") or "Поставщик"))
    supplier_title = escape(str(payload.get("supplier_product_title") or product_name))
    normal_price = _money(payload["baseline_price"], payload.get("currency"))
    current_price = _money(payload["current_price"], payload.get("currency"))
    drop_percent = escape(str(payload.get("drop_percent") or "0"))
    url = escape(str(payload.get("supplier_product_url") or ""), quote=True)

    identity_parts: list[str] = []
    if payload.get("merchant_sku"):
        identity_parts.append(f"SKU: {escape(str(payload['merchant_sku']))}")
    if payload.get("kaspi_product_id"):
        identity_parts.append(f"Kaspi ID: {escape(str(payload['kaspi_product_id']))}")

    lines = [
        "🔥 <b>Резко упала закупочная цена</b>",
        "",
        f"<b>{product_name}</b>",
    ]
    if identity_parts:
        lines.append(" · ".join(identity_parts))
    lines.extend(
        [
            f"Поставщик: {supplier_name}",
            f"Обычная цена: <s>{normal_price}</s>",
            f"Сейчас: <b>{current_price}</b>",
            f"Снижение: <b>−{drop_percent}%</b>",
            "",
            "Цена аномально низкая — можно рассмотреть закупку даже без текущего заказа.",
        ]
    )
    if url:
        lines.extend(["", f'<a href="{url}">Открыть товар у поставщика</a>'])
    if supplier_title != product_name:
        lines.extend(["", f"<i>Карточка поставщика: {supplier_title}</i>"])
    return "\n".join(lines)


async def _send_telegram_message(
    client: httpx.AsyncClient,
    *,
    settings: TelegramPriceAlertSettings,
    text: str,
) -> None:
    response = await client.post(
        f"{settings.api_base_url}/bot{settings.bot_token}/sendMessage",
        json={
            "chat_id": settings.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.is_success or body.get("ok") is not True:
        description = body.get("description") if isinstance(body, dict) else None
        reason = str(description or f"HTTP {response.status_code}")
        raise TelegramDeliveryError(f"Telegram отклонил уведомление: {reason}")


async def send_price_drop_message(
    client: httpx.AsyncClient,
    *,
    settings: TelegramPriceAlertSettings,
    payload: dict[str, object],
) -> None:
    await _send_telegram_message(
        client,
        settings=settings,
        text=format_price_drop_message(payload),
    )


def _pending_events(
    session_factory: SessionFactory,
    *,
    limit: int,
) -> list[tuple[object, dict[str, object]]]:
    with session_factory() as session:
        events = session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == PRICE_DROP_EVENT_TYPE,
                OutboxEvent.published_at.is_(None),
            )
            .order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc())
            .limit(limit)
        ).all()
        pending: list[tuple[object, dict[str, object]]] = []
        suppressed = False
        for event in events:
            payload = dict(event.payload_json)
            product_id = payload.get("product_id")
            try:
                product = session.get(Product, int(product_id))
            except (TypeError, ValueError):
                product = None
            if product is not None and product.sudden_price_alert_enabled:
                pending.append((event.id, payload))
                continue
            event.published_at = datetime.now(UTC)
            event.last_error = "suppressed: product price alert disabled"
            suppressed = True
        if suppressed:
            session.commit()
        return pending


def _record_publish_result(
    session_factory: SessionFactory,
    *,
    event_id: object,
    error: str | None,
) -> None:
    with session_factory() as session:
        event = session.get(OutboxEvent, event_id)
        if event is None or event.published_at is not None:
            return
        event.publish_attempts = int(event.publish_attempts or 0) + 1
        event.last_error = error
        if error is None:
            event.published_at = datetime.now(UTC)
        session.commit()


async def publish_pending_price_alerts(
    *,
    settings: TelegramPriceAlertSettings,
    session_factory: SessionFactory = SessionLocal,
    client: httpx.AsyncClient | None = None,
    limit: int = PUBLISH_BATCH_SIZE,
) -> tuple[int, int]:
    events = await asyncio.to_thread(
        _pending_events,
        session_factory,
        limit=limit,
    )
    sent = 0
    failed = 0
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=15)
    try:
        for event_id, payload in events:
            try:
                await send_price_drop_message(
                    active_client,
                    settings=settings,
                    payload=payload,
                )
            except Exception as exc:
                failed += 1
                await asyncio.to_thread(
                    _record_publish_result,
                    session_factory,
                    event_id=event_id,
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                )
            else:
                sent += 1
                await asyncio.to_thread(
                    _record_publish_result,
                    session_factory,
                    event_id=event_id,
                    error=None,
                )
    finally:
        if owns_client:
            await active_client.aclose()
    return sent, failed


async def price_alert_publisher_loop(stop_event: asyncio.Event) -> None:
    settings = TelegramPriceAlertSettings.from_environment()
    if settings is None:
        return

    async with httpx.AsyncClient(timeout=15) as client:
        while not stop_event.is_set():
            try:
                await publish_pending_price_alerts(
                    settings=settings,
                    client=client,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Individual errors are persisted on the outbox event. This
                # outer guard keeps a transient database failure from killing
                # the background publisher permanently.
                pass
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=PUBLISH_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue
