from __future__ import annotations

from typing import Any, Iterable


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _attrs(resource: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    value = resource.get("attributes")
    return value if isinstance(value, dict) else resource


def _identity_values(resource: dict[str, Any]) -> set[str]:
    attrs = _attrs(resource)
    values = {
        _clean(resource.get("id")),
        _clean(attrs.get("productId")),
        _clean(attrs.get("externalProductId")),
        _clean(attrs.get("offerCode")),
        _clean(attrs.get("merchantSku")),
        _clean(attrs.get("sku")),
        _clean(attrs.get("code")),
    }
    values.discard(None)
    return {value for value in values if value is not None}


def _product_title(resource: dict[str, Any]) -> str | None:
    attrs = _attrs(resource)
    for key in ("name", "title", "productName"):
        value = _clean(attrs.get(key))
        if value and value.lower() not in {"unknown product", "название не получено"}:
            return value
    return None


def recover_order_line_title(
    payload: dict[str, Any] | None,
    *,
    identities: Iterable[str | None],
) -> str | None:
    """Safe fallback for product titles from explicit order-entry resources only.

    Customer, delivery point and other order-level objects are intentionally ignored.
    Exact enrichment is performed through the archive v1.1.0 endpoint chain.
    """

    if not isinstance(payload, dict):
        return None
    attrs = _attrs(payload)
    entries = attrs.get("entries")
    if not isinstance(entries, list):
        return None

    wanted = {_clean(value) for value in identities}
    wanted.discard(None)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if wanted and not wanted.intersection(_identity_values(entry)):
            continue
        title = _product_title(entry)
        if title:
            return title
    return None
