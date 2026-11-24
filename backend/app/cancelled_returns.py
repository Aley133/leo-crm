"""Physical return tracking based on affirmative courier evidence, never estimates."""
from __future__ import annotations

import asyncio
import logging
import os
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
COMPENSATED = "cancelled_return_compensated"
RESOLVED_EVENTS = (RECEIVED, COMPENSATED)
ALERT = "cancelled_return_overdue"
# Revisit snapshots checked before the nested Kaspi Delivery fields were read.
CHECKED = "cancelled_return_evidence_checked_v2"
RETURN_STATUSES = ("cancelling", "cancelled")
WAIT = timedelta(days=10)
logger = logging.getLogger(__name__)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def handoff_time(payload: dict, observed_at: datetime) -> datetime | None:
    attrs = payload.get("attributes", payload)
    if not isinstance(attrs, dict):
        return None
    delivery = attrs.get("kaspiDelivery")
    nested_date = delivery.get("courierTransmissionDate") if isinstance(delivery, dict) else None
    for value in (nested_date, attrs.get("courierTransmissionDate")):
        if value is None or isinstance(value, bool):
            continue
        try:
            milliseconds = float(value)
            transmitted = datetime.fromtimestamp(milliseconds / 1000, UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if milliseconds > 0 and transmitted <= utc(observed_at):
            return transmitted
    # Canonical payloads preserve the original marketplaceStatus. Synthesized
    # SHIPPING must not be treated as proof of physical courier handoff.
    status = str(attrs.get("marketplaceStatus", attrs.get("status", ""))).upper()
    if status in {"SHIPPING", "HANDED_OVER_TO_COURIER"} and (
        "marketplaceStatus" in attrs or not attrs.get("leoClassificationSource")
        or attrs.get("leoClassificationSource") == "courier_transmission_date"
    ):
        return utc(observed_at)
    return None


def event_exists(*kinds: str):
    return exists().where(
        MarketplaceOrderEvent.marketplace_order_id == MarketplaceOrder.id,
        MarketplaceOrderEvent.event_type.in_(kinds),
    )


def find_event(order: MarketplaceOrder, *kinds: str):
    return next((event for event in order.events if event.event_type in kinds), None)


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
            MarketplaceRawPayload.workspace_id == order.workspace_id,
            MarketplaceRawPayload.marketplace_account_id == order.marketplace_account_id,
            MarketplaceRawPayload.external_object_id == order.external_order_id,
            MarketplaceRawPayload.payload_type == "order",
        ).order_by(MarketplaceRawPayload.received_at.desc(), MarketplaceRawPayload.id.desc()).limit(20)).all()
        for snapshot in snapshots:
            transmitted = handoff_time(snapshot.payload_json, utc(snapshot.received_at))
            if transmitted:
                handoff = add_event(order, HANDOFF, transmitted)
                break
    if order.status not in RETURN_STATUSES or handoff is None or find_event(order, TRACKED):
        return
    cancellations = [utc(event.occurred_at) for event in order.events
                     if event.current_status in RETURN_STATUSES
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
    return bool(order and order.status in RETURN_STATUSES and find_event(order, TRACKED)
                and not find_event(order, *RESOLVED_EVENTS))


def alert_message(payload: dict) -> str:
    return ("📦 <b>Не получен возврат после отмены</b>\n"
            f"Магазин: {escape(str(payload['account_name']))}\n"
            f"Заказ №{escape(str(payload['code']))}\n"
            f"{escape(str(payload['items']))}\n"
            f"Сумма заказа: {escape(str(payload['amount']))} ₸\n"
            f"Дата отмены: {escape(str(payload['cancelled_date']))}\n"
            "Прошло 10 дней или больше. Проверьте возврат; если товар не получен, обратитесь в Kaspi Гид.\n"
            "Когда товар или компенсация получены, отметьте исход во вкладке «Отменённые».")


def scan_returns(session: Session, *, after_id: int = 0, limit: int = 50,
                 now: datetime | None = None, alerts_start_at: datetime | None = None) -> int:
    now = utc(now or datetime.now(UTC))
    orders = session.scalars(select(MarketplaceOrder).where(
        MarketplaceOrder.status.in_(RETURN_STATUSES), MarketplaceOrder.id > after_id,
        ~event_exists(*RESOLVED_EVENTS),
        event_exists(TRACKED) | ~event_exists(CHECKED),
    ).options(selectinload(MarketplaceOrder.events), selectinload(MarketplaceOrder.lines),
              selectinload(MarketplaceOrder.account))
      .order_by(MarketplaceOrder.id).limit(limit).with_for_update(skip_locked=True)).all()
    for order in orders:
        observe_return(session, order, now=now, backfill=True)
        if not find_event(order, CHECKED):
            add_event(order, CHECKED, now)
        tracked = find_event(order, TRACKED)
        if tracked is None:
            continue
        due_at = utc(tracked.occurred_at) + WAIT
        if due_at > now or (alerts_start_at is not None and due_at < alerts_start_at):
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
    raw_alert_start = os.getenv("CANCELLED_RETURN_ALERTS_START_AT", "").strip()
    try:
        alerts_start_at = utc(datetime.fromisoformat(raw_alert_start)) if raw_alert_start else None
    except ValueError:
        logger.error("Invalid CANCELLED_RETURN_ALERTS_START_AT; cancelled return notifications are paused")
        alerts_start_at = datetime.max.replace(tzinfo=UTC)

    cursor = 0
    def cycle(after_id):
        with SessionLocal() as session:
            session.info["include_all_workspaces"] = True
            with session.begin():
                return scan_returns(session, after_id=after_id, alerts_start_at=alerts_start_at)
    while not stop_event.is_set():
        try:
            cursor = await asyncio.to_thread(cycle, cursor)
        except Exception:
            logger.exception("Cancelled returns scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except TimeoutError:
            pass
