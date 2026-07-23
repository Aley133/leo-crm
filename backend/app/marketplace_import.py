from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    MarketplaceImportCheckpoint,
    MarketplaceOrder,
    MarketplaceOrderEvent,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    MarketplaceRawPayload,
)


KASPI_STATUS_MAP: dict[str, str] = {
    "NEW": MarketplaceOrderStatus.NEW.value,
    "SIGN_REQUIRED": MarketplaceOrderStatus.NEW.value,
    "APPROVED_BY_BANK": MarketplaceOrderStatus.ACCEPTED.value,
    "ACCEPTED_BY_MERCHANT": MarketplaceOrderStatus.ACCEPTED.value,
    "PICKUP": MarketplaceOrderStatus.ACCEPTED.value,
    "ASSEMBLE": MarketplaceOrderStatus.ASSEMBLY.value,
    "ASSEMBLY": MarketplaceOrderStatus.ASSEMBLY.value,
    "HANDOVER": MarketplaceOrderStatus.HANDOVER.value,
    "SHIPPING": MarketplaceOrderStatus.SHIPPING.value,
    "HANDED_OVER_TO_COURIER": MarketplaceOrderStatus.SHIPPING.value,
    "CANCELLING": MarketplaceOrderStatus.CANCELLING.value,
    "DELIVERED": MarketplaceOrderStatus.DELIVERED.value,
    "COMPLETED": MarketplaceOrderStatus.DELIVERED.value,
    "ARCHIVE": MarketplaceOrderStatus.DELIVERED.value,
    "ARCHIVED": MarketplaceOrderStatus.DELIVERED.value,
    "CANCELLED": MarketplaceOrderStatus.CANCELLED.value,
    "CANCELED": MarketplaceOrderStatus.CANCELLED.value,
    "KASPI_DELIVERY_RETURN_REQUESTED": MarketplaceOrderStatus.RETURNED.value,
    "RETURNED": MarketplaceOrderStatus.RETURNED.value,
}

KASPI_STATE_FALLBACK_MAP: dict[str, str] = {
    "NEW": MarketplaceOrderStatus.NEW.value,
    "PICKUP": MarketplaceOrderStatus.ACCEPTED.value,
    "DELIVERY": MarketplaceOrderStatus.ACCEPTED.value,
    "KASPI_DELIVERY": MarketplaceOrderStatus.ACCEPTED.value,
    "ARCHIVE": MarketplaceOrderStatus.DELIVERED.value,
    "ARCHIVED": MarketplaceOrderStatus.DELIVERED.value,
}

_PLACEHOLDER_TITLES = {"", "unknown product", "название не получено"}


@dataclass(frozen=True, slots=True)
class NormalizedOrderLine:
    external_line_id: str
    external_product_id: str | None
    merchant_sku: str | None
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    external_order_id: str
    external_code: str | None
    status: str
    original_status: str
    source_revision: str | None
    currency: str
    total_amount: Decimal
    ordered_at: datetime | None
    planned_delivery_at: datetime | None
    delivered_at: datetime | None
    source_updated_at: datetime | None
    lines: tuple[NormalizedOrderLine, ...]


@dataclass(frozen=True, slots=True)
class ImportResult:
    order_id: int
    created: bool
    changed: bool
    raw_payload_created: bool


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _is_placeholder_title(value: str | None) -> bool:
    return (value or "").strip().casefold() in _PLACEHOLDER_TITLES


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _as_decimal(value: Any, *, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_kaspi_status(attributes: dict[str, Any]) -> tuple[str, str]:
    raw_status = _first(attributes, "status", "orderStatus")
    if raw_status not in (None, ""):
        original = str(raw_status).strip()
        return KASPI_STATUS_MAP.get(original.upper(), MarketplaceOrderStatus.UNKNOWN.value), original

    raw_state = _first(attributes, "state", "fulfillmentState", "deliveryStatus")
    if raw_state not in (None, ""):
        original = str(raw_state).strip()
        return KASPI_STATE_FALLBACK_MAP.get(original.upper(), MarketplaceOrderStatus.UNKNOWN.value), original

    return MarketplaceOrderStatus.UNKNOWN.value, "UNKNOWN"


def normalize_kaspi_order(payload: dict[str, Any]) -> NormalizedOrder:
    attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else payload
    external_order_id = str(_first(payload, "id") or _first(attributes, "id", "orderId", "code") or "").strip()
    if not external_order_id:
        raise ValueError("Kaspi order payload has no external order identity")

    normalized_status, original_status = _normalize_kaspi_status(attributes)
    raw_lines = _first(attributes, "entries", "orderEntries", "lines") or []
    lines: list[NormalizedOrderLine] = []
    for index, raw_line in enumerate(raw_lines):
        if not isinstance(raw_line, dict):
            raise ValueError("Kaspi order line must be an object")
        line_attributes = raw_line.get("attributes") if isinstance(raw_line.get("attributes"), dict) else raw_line
        quantity = int(_first(line_attributes, "quantity", "qty") or 1)
        unit_price = _as_decimal(_first(line_attributes, "basePrice", "unitPrice", "price"))
        line_total = _as_decimal(
            _first(line_attributes, "totalPrice", "lineTotal"),
            default=str(unit_price * quantity),
        )
        external_line_id = str(
            _first(raw_line, "id")
            or _first(line_attributes, "id", "entryId")
            or f"{external_order_id}:{index}"
        )
        lines.append(
            NormalizedOrderLine(
                external_line_id=external_line_id,
                external_product_id=_clean_text(_first(line_attributes, "productId", "externalProductId")),
                merchant_sku=_clean_text(_first(line_attributes, "offerCode", "merchantSku", "sku", "code")),
                title=_clean_text(_first(line_attributes, "name", "title", "productName")) or "Unknown product",
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )

    total_amount = _as_decimal(
        _first(attributes, "totalPrice", "totalAmount", "amount"),
        default=str(sum((line.line_total for line in lines), Decimal("0"))),
    )
    external_code_value = _first(attributes, "code", "orderCode")
    source_revision_value = _first(attributes, "revision", "version", "updatedAt")

    return NormalizedOrder(
        external_order_id=external_order_id,
        external_code=_clean_text(external_code_value),
        status=normalized_status,
        original_status=original_status,
        source_revision=_clean_text(source_revision_value),
        currency=str(_first(attributes, "currency") or "KZT"),
        total_amount=total_amount,
        ordered_at=_as_datetime(_first(attributes, "creationDate", "orderedAt", "createdAt")),
        planned_delivery_at=_as_datetime(_first(attributes, "plannedDeliveryDate", "plannedDeliveryAt")),
        delivered_at=_as_datetime(_first(attributes, "deliveryDate", "deliveredAt")),
        source_updated_at=_as_datetime(_first(attributes, "updatedAt", "modifiedAt")),
        lines=tuple(lines),
    )


def _find_existing_line(
    existing_lines: list[MarketplaceOrderLine],
    normalized: NormalizedOrderLine,
    *,
    single_incoming: bool,
) -> MarketplaceOrderLine | None:
    for line in existing_lines:
        if str(line.external_line_id or "") == normalized.external_line_id:
            return line
    if normalized.merchant_sku:
        for line in existing_lines:
            if _clean_text(line.merchant_sku) == normalized.merchant_sku:
                return line
    if normalized.external_product_id:
        for line in existing_lines:
            if _clean_text(line.external_product_id) == normalized.external_product_id:
                return line
    if single_incoming and len(existing_lines) == 1:
        return existing_lines[0]
    return None


def _merge_line(line: MarketplaceOrderLine, incoming: NormalizedOrderLine) -> bool:
    changed = False
    if incoming.external_line_id and line.external_line_id != incoming.external_line_id:
        line.external_line_id = incoming.external_line_id
        changed = True
    if incoming.external_product_id and line.external_product_id != incoming.external_product_id:
        line.external_product_id = incoming.external_product_id
        changed = True
    if incoming.merchant_sku and line.merchant_sku != incoming.merchant_sku:
        line.merchant_sku = incoming.merchant_sku
        changed = True
    if not _is_placeholder_title(incoming.title) and line.title != incoming.title:
        line.title = incoming.title
        changed = True
    if line.quantity != incoming.quantity:
        line.quantity = incoming.quantity
        changed = True
    if Decimal(line.unit_price) != incoming.unit_price:
        line.unit_price = incoming.unit_price
        changed = True
    if Decimal(line.line_total) != incoming.line_total:
        line.line_total = incoming.line_total
        changed = True
    return changed


def import_kaspi_order(
    session: Session,
    *,
    marketplace_account_id: int,
    payload: dict[str, Any],
    import_execution_id: UUID | None = None,
    checkpoint_stream: str = "orders",
    checkpoint_cursor: str | None = None,
    checkpoint_watermark_at: datetime | None = None,
) -> ImportResult:
    normalized = normalize_kaspi_order(payload)
    content_hash = _canonical_hash(payload)

    raw_payload = session.scalar(
        select(MarketplaceRawPayload).where(
            MarketplaceRawPayload.marketplace_account_id == marketplace_account_id,
            MarketplaceRawPayload.payload_type == "order",
            MarketplaceRawPayload.external_object_id == normalized.external_order_id,
            MarketplaceRawPayload.content_hash == content_hash,
        )
    )
    raw_payload_created = raw_payload is None
    if raw_payload is None:
        session.add(
            MarketplaceRawPayload(
                marketplace_account_id=marketplace_account_id,
                import_execution_id=import_execution_id,
                payload_type="order",
                external_object_id=normalized.external_order_id,
                content_hash=content_hash,
                payload_json=payload,
            )
        )

    order = session.scalar(
        select(MarketplaceOrder).where(
            MarketplaceOrder.marketplace_account_id == marketplace_account_id,
            MarketplaceOrder.external_order_id == normalized.external_order_id,
        )
    )
    created = order is None
    previous_status: str | None = None

    if order is None:
        order = MarketplaceOrder(
            marketplace_account_id=marketplace_account_id,
            external_order_id=normalized.external_order_id,
            external_code=normalized.external_code,
            status=normalized.status,
            original_status=normalized.original_status,
            source_revision=normalized.source_revision,
            currency=normalized.currency,
            total_amount=normalized.total_amount,
            ordered_at=normalized.ordered_at,
            planned_delivery_at=normalized.planned_delivery_at,
            delivered_at=normalized.delivered_at,
            source_updated_at=normalized.source_updated_at,
            version=1,
        )
        session.add(order)
        session.flush()
        changed = True
    else:
        previous_status = order.status
        before = (
            order.external_code,
            order.status,
            order.original_status,
            order.source_revision,
            str(order.currency),
            Decimal(order.total_amount),
            order.ordered_at,
            order.planned_delivery_at,
            order.delivered_at,
            order.source_updated_at,
        )
        after = (
            normalized.external_code,
            normalized.status,
            normalized.original_status,
            normalized.source_revision,
            normalized.currency,
            normalized.total_amount,
            normalized.ordered_at,
            normalized.planned_delivery_at,
            normalized.delivered_at,
            normalized.source_updated_at,
        )
        changed = before != after
        if changed:
            order.external_code = normalized.external_code
            order.status = normalized.status
            order.original_status = normalized.original_status
            order.source_revision = normalized.source_revision
            order.currency = normalized.currency
            order.total_amount = normalized.total_amount
            order.ordered_at = normalized.ordered_at
            order.planned_delivery_at = normalized.planned_delivery_at
            order.delivered_at = normalized.delivered_at
            order.source_updated_at = normalized.source_updated_at
            order.version += 1

    existing_lines = list(order.lines)
    single_incoming = len(normalized.lines) == 1
    for incoming in normalized.lines:
        line = _find_existing_line(existing_lines, incoming, single_incoming=single_incoming)
        if line is None:
            line = MarketplaceOrderLine(
                external_line_id=incoming.external_line_id,
                external_product_id=incoming.external_product_id,
                merchant_sku=incoming.merchant_sku,
                title=incoming.title,
                quantity=incoming.quantity,
                unit_price=incoming.unit_price,
                line_total=incoming.line_total,
            )
            order.lines.append(line)
            existing_lines.append(line)
            changed = True
        elif _merge_line(line, incoming):
            changed = True

    # Kaspi may temporarily return an order without included entries. Existing
    # enriched lines are intentionally retained; incomplete observations must not
    # destroy title, SKU, catalogue link or procurement history.

    if created or previous_status != normalized.status:
        event_key = f"status:{normalized.source_revision or content_hash}:{normalized.status}"
        existing_event = session.scalar(
            select(MarketplaceOrderEvent).where(
                MarketplaceOrderEvent.marketplace_order_id == order.id,
                MarketplaceOrderEvent.source_event_key == event_key,
            )
        )
        if existing_event is None:
            order.events.append(
                MarketplaceOrderEvent(
                    source_event_key=event_key,
                    event_type="status_changed" if previous_status is not None else "order_imported",
                    previous_status=previous_status,
                    current_status=normalized.status,
                    occurred_at=normalized.source_updated_at or datetime.now(UTC),
                    metadata_json={"original_status": normalized.original_status},
                )
            )

    checkpoint = session.scalar(
        select(MarketplaceImportCheckpoint).where(
            MarketplaceImportCheckpoint.marketplace_account_id == marketplace_account_id,
            MarketplaceImportCheckpoint.stream_name == checkpoint_stream,
        )
    )
    if checkpoint is None:
        checkpoint = MarketplaceImportCheckpoint(
            marketplace_account_id=marketplace_account_id,
            stream_name=checkpoint_stream,
        )
        session.add(checkpoint)
    if checkpoint_cursor is not None:
        checkpoint.cursor = checkpoint_cursor
    if checkpoint_watermark_at is not None:
        checkpoint.watermark_at = checkpoint_watermark_at

    session.flush()
    return ImportResult(
        order_id=order.id,
        created=created,
        changed=changed,
        raw_payload_created=raw_payload_created,
    )
