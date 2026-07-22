from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .models import (
    SellerOrderDelivery,
    SellerOrderFacts,
    SellerOrderLine,
    SellerOrderMarker,
    SellerOrderSnapshot,
    SellerOrderStep,
    SellerOrderWarehouse,
)
from .stage_resolver import resolve_seller_stage


def map_seller_order_facts(payload: dict[str, Any]) -> SellerOrderFacts:
    """Map either a raw Seller GraphQL orderDetail or importer attributes into facts."""
    order_detail = _unwrap_order_detail(payload)
    delivery = _as_dict(order_detail.get("delivery"))

    steps = _map_steps(order_detail.get("orderSteps"))
    markers = _map_markers(order_detail.get("markers"))

    return SellerOrderFacts(
        order_code=_optional_text(order_detail.get("code") or order_detail.get("orderCode")),
        state=str(order_detail.get("state") or "UNKNOWN").strip().upper(),
        status=str(
            order_detail.get("status")
            or order_detail.get("orderStatus")
            or "UNKNOWN"
        ).strip().upper(),
        preorder=order_detail.get("preOrder") is True,
        is_order_arrived=delivery.get("isOrderArrived") is True,
        kd_assembled=delivery.get("kdAssembled") is True,
        kd_transmitted_to_courier=delivery.get("kdTransmittedToCourier") is True,
        steps=steps,
        marker_names=tuple(marker.name for marker in markers),
    )


def map_seller_order_snapshot(
    payload: dict[str, Any],
    *,
    merchant_id: str | None = None,
) -> SellerOrderSnapshot:
    """Normalize a Seller GraphQL response or Browser Agent job result.

    Accepted envelopes include the raw GraphQL response, the Browser Agent
    ``result`` object, the complete job response, and importer ``attributes``.
    """
    order_detail = _unwrap_order_detail(payload)
    facts = map_seller_order_facts(payload)
    order_code = facts.order_code or _find_text(payload, ("order_code", "orderCode"))
    if order_code is None:
        raise ValueError("Kaspi Seller order snapshot requires an order code")

    delivery_payload = _as_dict(order_detail.get("delivery"))
    warehouse_payload = _as_dict(order_detail.get("warehouse"))
    customer = _as_dict(order_detail.get("customer"))
    recipient = _as_dict(order_detail.get("recipient"))

    delivery = SellerOrderDelivery(
        mode=_optional_text(delivery_payload.get("mode")),
        assembled=delivery_payload.get("kdAssembled") is True,
        transmitted_to_courier=delivery_payload.get("kdTransmittedToCourier") is True,
        order_arrived=delivery_payload.get("isOrderArrived") is True,
        returned_to_warehouse=delivery_payload.get("isReturnedToWarehouse") is True,
        transmission_planned_at=_optional_text(
            delivery_payload.get("transmissionPlanningDate")
        ),
        planned_delivery_at=_optional_text(delivery_payload.get("plannedDeliveryDate")),
        planned_point_delivery_at=_optional_text(
            delivery_payload.get("plannedPointDeliveryDate")
        ),
        assembled_at=_optional_text(delivery_payload.get("assembleDate")),
        actual_delivery_at=_optional_text(delivery_payload.get("actualDeliveryDate")),
    )

    return SellerOrderSnapshot(
        merchant_id=_optional_text(merchant_id) or _find_merchant_id(payload),
        order_code=order_code,
        state=facts.state,
        status=facts.status,
        stage=resolve_seller_stage(facts),
        preorder=facts.preorder,
        creation_time=_optional_text(order_detail.get("creationTime")),
        modification_time=_optional_text(order_detail.get("modificationTime")),
        customer_name=_party_name(customer),
        recipient_name=_party_name(recipient),
        delivery=delivery,
        warehouse=_map_warehouse(warehouse_payload) if warehouse_payload else None,
        lines=_map_lines(order_detail.get("entries")),
        steps=facts.steps,
        markers=_map_markers(order_detail.get("markers")),
        schema_version=_find_text(payload, ("schema_version", "schemaVersion")),
    )


def _map_steps(value: Any) -> tuple[SellerOrderStep, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        SellerOrderStep.from_payload(step)
        for step in value
        if isinstance(step, dict)
    )


def _map_markers(value: Any) -> tuple[SellerOrderMarker, ...]:
    if not isinstance(value, list):
        return ()
    markers: list[SellerOrderMarker] = []
    for marker in value:
        if not isinstance(marker, dict):
            continue
        name = _optional_text(marker.get("marker"))
        if name is None:
            continue
        markers.append(
            SellerOrderMarker(
                name=name.strip().upper(),
                creation_time=_optional_text(marker.get("creationTime")),
            )
        )
    return tuple(markers)


def _map_lines(value: Any) -> tuple[SellerOrderLine, ...]:
    if not isinstance(value, list):
        return ()
    lines: list[SellerOrderLine] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        merchant_product = _as_dict(entry.get("merchantProduct"))
        product = _as_dict(entry.get("product"))
        entry_id = entry.get("entryId")
        if not isinstance(entry_id, (int, str)) or isinstance(entry_id, bool):
            entry_id = None
        lines.append(
            SellerOrderLine(
                entry_id=entry_id,
                merchant_sku=_optional_text(merchant_product.get("code")),
                product_code=_optional_text(product.get("code")),
                barcode=_optional_text(merchant_product.get("barcode")),
                title=_optional_text(merchant_product.get("name"))
                or _optional_text(product.get("name")),
                quantity=_int_value(entry.get("quantity"), default=0),
                total_price=_optional_int(entry.get("totalPrice")),
            )
        )
    return tuple(lines)


def _map_warehouse(value: dict[str, Any]) -> SellerOrderWarehouse:
    city = _as_dict(value.get("city"))
    kaspi_delivery = _as_dict(value.get("kaspiDelivery"))
    return SellerOrderWarehouse(
        name=_optional_text(value.get("name")),
        city_id=_optional_text(city.get("id")),
        city_name=_optional_text(city.get("name")),
        pickup_type=_optional_text(kaspi_delivery.get("pickupType")),
    )


def _party_name(value: dict[str, Any]) -> str | None:
    parts = [
        text.strip()
        for text in (
            _optional_text(value.get("firstName")),
            _optional_text(value.get("lastName")),
        )
        if text is not None and text.strip()
    ]
    return " ".join(parts) or None


def _unwrap_order_detail(payload: dict[str, Any]) -> dict[str, Any]:
    for candidate in _walk_envelopes(payload):
        data = candidate.get("data")
        if isinstance(data, dict):
            merchant = data.get("merchant")
            if isinstance(merchant, dict):
                detail = merchant.get("orderDetail")
                if isinstance(detail, dict):
                    return detail

        detail = candidate.get("orderDetail")
        if isinstance(detail, dict):
            return detail

        attributes = candidate.get("attributes")
        if isinstance(attributes, dict):
            return attributes

    return payload


def _walk_envelopes(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    queue: deque[dict[str, Any]] = deque([payload])
    seen: set[int] = set()
    while queue:
        candidate = queue.popleft()
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        yield candidate
        for key in ("result", "details_response"):
            nested = candidate.get(key)
            if isinstance(nested, dict):
                queue.append(nested)


def _find_merchant_id(payload: dict[str, Any]) -> str | None:
    direct = _find_text(payload, ("merchant_id", "merchantUid", "merchantId"))
    if direct is not None:
        return direct
    for candidate in _walk_envelopes(payload):
        data = candidate.get("data")
        if not isinstance(data, dict):
            continue
        merchant = data.get("merchant")
        if isinstance(merchant, dict):
            value = _optional_text(merchant.get("id"))
            if value is not None:
                return value
    return None


def _find_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for candidate in _walk_envelopes(payload):
        for key in keys:
            value = _optional_text(candidate.get(key))
            if value is not None:
                return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _int_value(value: Any, *, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
