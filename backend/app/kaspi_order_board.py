from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("preorder", "Предзаказ"),
    ("assembly", "Упаковка"),
    ("handover", "Передача"),
    ("shipping", "Передан в доставку"),
    ("cancelling", "Отмена в процессе"),
    ("cancelled", "Отменён"),
    ("delivered", "Завершён"),
    ("unknown", "Прочее"),
)


@dataclass(frozen=True, slots=True)
class KaspiOrderClassification:
    stage: str
    order_type: str
    source: str


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
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).astimezone(
        _zone(timezone_name)
    )


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
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _handoff_deadline(started_at: datetime, cutoff_hour: int) -> datetime:
    candidate = started_at.replace(
        hour=cutoff_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if started_at >= candidate:
        candidate += timedelta(days=1)
    return candidate


def classify_kaspi_order_details(
    attributes: dict[str, Any],
    *,
    timezone_name: str = "Asia/Almaty",
    now: datetime | None = None,
    handoff_cutoff_hour: int = 21,
    history_record: dict[str, Any] | None = None,
) -> KaspiOrderClassification:
    """Archive v1.0.1 rules adapted only at the Commerce Core boundary."""

    status = str(attributes.get("status") or "").upper()
    state = str(attributes.get("state") or "").upper()
    order_type = "preorder" if attributes.get("preOrder") is True else "stock"

    if status == "CANCELLING":
        return KaspiOrderClassification("cancelling", order_type, "kaspi_status")
    if status == "CANCELLED":
        return KaspiOrderClassification("cancelled", order_type, "kaspi_status")
    if status == "COMPLETED":
        return KaspiOrderClassification("delivered", order_type, "kaspi_status")

    delivery_cost = _number(attributes.get("deliveryCostForSeller"))
    if delivery_cost <= 0:
        if state in {"NEW", "SIGN_REQUIRED", "PICKUP", "DELIVERY", "KASPI_DELIVERY"}:
            stage = "preorder" if attributes.get("preOrder") is True else "assembly"
            return KaspiOrderClassification(stage, order_type, "preorder_flag")
        return KaspiOrderClassification("unknown", order_type, "unsupported_state")

    if _datetime_ms(attributes.get("courierTransmissionDate"), timezone_name):
        return KaspiOrderClassification(
            "shipping",
            order_type,
            "courier_transmission_date",
        )

    local_now = _local_now(timezone_name, now)
    planned = _datetime_ms(
        attributes.get("courierTransmissionPlanningDate"),
        timezone_name,
    )
    if planned is not None:
        if planned.date() < local_now.date():
            return KaspiOrderClassification(
                "shipping",
                order_type,
                "planned_transmission_date",
            )
        if planned.date() > local_now.date():
            return KaspiOrderClassification(
                "handover",
                order_type,
                "planned_transmission_date",
            )
        cutoff = planned.replace(
            hour=handoff_cutoff_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        stage = "shipping" if local_now >= cutoff else "handover"
        return KaspiOrderClassification(
            stage,
            order_type,
            "planned_transmission_date",
        )

    transfer_started = _parse_iso(
        (history_record or {}).get("transfer_started_at"),
        timezone_name,
    )
    if transfer_started is not None:
        stage = (
            "shipping"
            if local_now >= _handoff_deadline(transfer_started, handoff_cutoff_hour)
            else "handover"
        )
        return KaspiOrderClassification(
            stage,
            order_type,
            "delivery_cost_transition",
        )

    created = _datetime_ms(attributes.get("creationDate"), timezone_name)
    if created is not None and created.date() < local_now.date():
        return KaspiOrderClassification(
            "shipping",
            order_type,
            "first_import_old_order",
        )
    return KaspiOrderClassification(
        "handover",
        order_type,
        "first_import_today",
    )


def classify_kaspi_order(
    attributes: dict[str, Any],
    *,
    timezone_name: str = "Asia/Almaty",
    now: datetime | None = None,
    handoff_cutoff_hour: int = 21,
    history_record: dict[str, Any] | None = None,
) -> str:
    return classify_kaspi_order_details(
        attributes,
        timezone_name=timezone_name,
        now=now,
        handoff_cutoff_hour=handoff_cutoff_hour,
        history_record=history_record,
    ).stage
