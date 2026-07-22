from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("preorder", "Предзаказ"),
    ("assembly", "Упаковка"),
    ("handover", "Передача"),
    ("shipping", "Переданы на доставку"),
    ("cancelled", "Отменены при доставке"),
    ("delivered", "Завершены"),
    ("returned", "Возвраты"),
    ("unknown", "Прочее"),
)


def _zone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _datetime_ms(value: Any, timezone_name: str) -> datetime | None:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).astimezone(_zone(timezone_name))


def _local_now(timezone_name: str, now: datetime | None) -> datetime:
    zone = _zone(timezone_name)
    if now is None:
        return datetime.now(tz=zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _parse_iso(value: Any, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    zone = _zone(timezone_name)
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _handoff_deadline(started_at: datetime, cutoff_hour: int) -> datetime:
    candidate = started_at.replace(hour=cutoff_hour, minute=0, second=0, microsecond=0)
    if started_at >= candidate:
        candidate += timedelta(days=1)
    return candidate


def classify_kaspi_order(
    attributes: dict[str, Any],
    *,
    timezone_name: str = "Asia/Almaty",
    now: datetime | None = None,
    handoff_cutoff_hour: int = 21,
    history_record: dict[str, Any] | None = None,
) -> str:
    """Authoritative LEO stage derived only from official Kaspi Orders API facts."""

    status = str(attributes.get("status") or "").upper()
    state = str(attributes.get("state") or "").upper()

    if status in {"CANCELLING", "CANCELLED", "CANCELED"}:
        return "cancelled"
    if status in {"COMPLETED", "DELIVERED", "ARCHIVED"}:
        return "delivered"
    if status in {"RETURNED", "KASPI_DELIVERY_RETURN_REQUESTED"}:
        return "returned"

    delivery_cost = _number(attributes.get("deliveryCostForSeller"))
    if delivery_cost <= 0:
        if state in {"NEW", "SIGN_REQUIRED", "PICKUP", "DELIVERY", "KASPI_DELIVERY"}:
            return "preorder" if attributes.get("preOrder") is True else "assembly"
        return "unknown"

    actual = _datetime_ms(attributes.get("courierTransmissionDate"), timezone_name)
    if actual is not None:
        return "shipping"

    local_now = _local_now(timezone_name, now)
    planned = _datetime_ms(attributes.get("courierTransmissionPlanningDate"), timezone_name)
    if planned is not None:
        if planned.date() < local_now.date():
            return "shipping"
        if planned.date() > local_now.date():
            return "handover"
        cutoff = planned.replace(hour=handoff_cutoff_hour, minute=0, second=0, microsecond=0)
        return "shipping" if local_now >= cutoff else "handover"

    transfer_started = _parse_iso((history_record or {}).get("transfer_started_at"), timezone_name)
    if transfer_started is not None:
        return "shipping" if local_now >= _handoff_deadline(transfer_started, handoff_cutoff_hour) else "handover"

    created = _datetime_ms(attributes.get("creationDate"), timezone_name)
    if created is not None and created.date() < local_now.date():
        return "shipping"
    return "handover"
