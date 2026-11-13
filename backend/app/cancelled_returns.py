"""Physical return tracking based on affirmative courier evidence, never estimates."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from .db import SessionLocal
from .models import MarketplaceOrder, MarketplaceOrderEvent, MarketplaceRawPayload, OutboxEvent

HANDOFF = "courier_handoff_confirmed"
TRACKED = "cancelled_return_tracked"
RECEIVED = "cancelled_return_received"
ALERT = "cancelled_return_overdue"
CHECKED = "cancelled_return_evidence_checked"
WAIT = timedelta(days=10)
logger = logging.getLogger(__name__)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def handoff_time(payload: dict, observed_at: datetime) -> datetime | None:
    attrs = payload.get("attributes", payload)
    if not isinstance(attrs, dict):
        return None
    value = attrs.get("courierTransmissionDate")
    try:
        transmitted = datetime.fromtimestamp(float(value) / 1000, UTC)
        if 0 < float(value) and transmitted <= utc(observed_at):
            return transmitted
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    # Canonical payloads preserve the original marketplaceStatus. Synthesized
    # SHIPPING must not be treated as proof of physical courier handoff.
    status = str(attrs.get("marketplaceStatus", attrs.get("status", ""))).upper()
    if status in {"SHIPPING", "HANDED_OVER_TO_COURIER"} and (
        "marketplaceStatus" in attrs or not attrs.get("leoClassificationSource")
        or attrs.get("leoClassificationSource") == "courier_transmission_date"
    ):
        return utc(observed_at)
    return None


def event_exists(kind: str):
    return exists().where(
        MarketplaceOrderEvent.marketplace_order_id == MarketplaceOrder.id,
        MarketplaceOrderEvent.event_type == kind,
    )


def find_event(order: MarketplaceOrder, kind: str):
    return next((event for event in order.events if event.event_type == kind), None)


def add_event(order: MarketplaceOrder, kind: str, when: datetime, metadata=None):
    event = MarketplaceOrderEvent(
        workspace_id=order.workspace_id, source_event_key=kind,
        event_type=kind, occurred_at=when, metadata_json=metadata or {},
    )
    order.events.append(event)
    return event


def observe_return(session: Session, order: MarketplaceOrder, payload: dict | None = None,
                   *, now: datetime | None = None, backfill: bool = False) -> None:
    now = utc(now or datetime.now(UTC))
    handoff = find_event(order, HANDOFF)
    if handoff is None and payload is not None:
        transmitted = handoff_time(payload, now)
        if transmitted:
            handoff = add_event(order, HANDOFF, transmitted)
    if handoff is None and backfill:
        snapshots = session.scalars(select(MarketplaceRawPayload).where(
            MarketplaceRawPayload.marketplace_account_id == order.marketplace_account_id,
            MarketplaceRawPayload.external_object_id == order.external_order_id,
            MarketplaceRawPayload.payload_type == "order",
        ).order_by(MarketplaceRawPayload.id).limit(20)).all()
        for snapshot in snapshots:
            transmitted = handoff_time(snapshot.payload_json, utc(snapshot.received_at))
            if transmitted:
                handoff = add_event(order, HANDOFF, transmitted)
                break
    if order.status != "cancelled" or handoff is None or find_event(order, TRACKED):
        return
    cancellations = [utc(event.occurred_at) for event in order.events
                     if event.current_status == "cancelled"
                     and event.event_type in {"status_changed", "order_imported"}
                     and utc(event.occurred_at) >= utc(handoff.occurred_at)]
    if not cancellations:
        return
    cancelled_at = min(cancellations)
    add_event(order, TRACKED, cancelled_at, {
        "handoff_at": utc(handoff.occurred_at).isoformat(),
        "due_at": (cancelled_at + WAIT).isoformat(),
    })


def current_alert(session: Session, payload: dict) -> bool:
    order = session.get(MarketplaceOrder, int(payload["order_id"]))
    return bool(order and order.status == "cancelled" and find_event(order, TRACKED)
                and not find_event(order, RECEIVED))


def alert_message(payload: dict) -> str:
    return ("📦 <b>Не получен возврат после отмены</b>\n"
            f"Магазин: {escape(str(payload['account_name']))}\n"
            f"Заказ №{escape(str(payload['code']))}\n"
            f"{escape(str(payload['items']))}\n"
            f"Сумма заказа: {escape(str(payload['amount']))} ₸\n"
            f"Дата отмены: {escape(str(payload['cancelled_date']))}\n"
            "Прошло 10 дней или больше. Проверьте возврат; если товар не получен, обратитесь в Kaspi Гид.\n"
            "После физического получения нажмите «Получил» во вкладке «Отменённые».")


def scan_returns(session: Session, *, after_id: int = 0, limit: int = 50,
                 now: datetime | None = None) -> int:
    now = utc(now or datetime.now(UTC))
    orders = session.scalars(select(MarketplaceOrder).where(
        MarketplaceOrder.status == "cancelled", MarketplaceOrder.id > after_id,
        ~event_exists(RECEIVED),
        event_exists(TRACKED) | ~event_exists(CHECKED),
    ).options(selectinload(MarketplaceOrder.events), selectinload(MarketplaceOrder.lines),
              selectinload(MarketplaceOrder.account))
      .order_by(MarketplaceOrder.id).limit(limit).with_for_update(skip_locked=True)).all()
    for order in orders:
        observe_return(session, order, now=now, backfill=True)
        if not find_event(order, CHECKED):
            add_event(order, CHECKED, now)
        tracked = find_event(order, TRACKED)
        if tracked is None or now < utc(tracked.occurred_at) + WAIT:
            continue
        key = f"cancelled-return:{order.workspace_id}:{order.id}:10d"
        if session.scalar(select(OutboxEvent.id).where(OutboxEvent.idempotency_key == key)):
            continue
        session.add(OutboxEvent(
            workspace_id=order.workspace_id, aggregate_type="marketplace_order",
            aggregate_id=str(order.id), event_type=ALERT, idempotency_key=key,
            payload_json={
                "order_id": order.id, "workspace_id": order.workspace_id,
                "account_name": order.account.display_name, "code": order.external_code or order.external_order_id,
                "items": "; ".join(f"{line.title} × {line.quantity}" for line in order.lines)[:1500],
                "amount": str(order.total_amount),
                "cancelled_date": utc(tracked.occurred_at).astimezone(ZoneInfo("Asia/Qyzylorda")).strftime("%d.%m.%Y"),
            },
        ))
    return orders[-1].id if len(orders) == limit else 0


async def returns_loop(stop_event: asyncio.Event) -> None:
    cursor = 0
    def cycle(after_id):
        with SessionLocal() as session:
            session.info["include_all_workspaces"] = True
            with session.begin():
                return scan_returns(session, after_id=after_id)
    while not stop_event.is_set():
        try:
            cursor = await asyncio.to_thread(cycle, cursor)
        except Exception:
            logger.exception("Cancelled returns scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except TimeoutError:
            pass
