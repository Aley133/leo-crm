from __future__ import annotations

from typing import Any, Iterable


_TITLE_KEYS = (
    "name",
    "title",
    "productName",
    "productTitle",
    "offerName",
    "displayName",
)
_IDENTITY_KEYS = (
    "productId",
    "externalProductId",
    "offerCode",
    "merchantSku",
    "sku",
    "code",
    "id",
)


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def recover_order_line_title(
    payload: dict[str, Any] | None,
    *,
    identities: Iterable[str | None],
) -> str | None:
    """Recover a human product title from immutable Kaspi raw evidence.

    Kaspi can return the title in an entry, offer, merchantProduct, product or
    masterProduct object. We first prefer objects matching the line identity and
    then fall back to the first credible product title in the order payload.
    """

    if not isinstance(payload, dict):
        return None

    wanted = {_clean(value) for value in identities}
    wanted.discard(None)
    fallback: str | None = None

    for node in _walk(payload):
        titles = [_clean(node.get(key)) for key in _TITLE_KEYS]
        title = next(
            (
                value
                for value in titles
                if value is not None and value.lower() != "unknown product"
            ),
            None,
        )
        if title is None:
            continue

        if fallback is None:
            fallback = title

        node_identities = {_clean(node.get(key)) for key in _IDENTITY_KEYS}
        node_identities.discard(None)
        if wanted and wanted.intersection(node_identities):
            return title

    return fallback
