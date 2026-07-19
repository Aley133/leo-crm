from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def offer_fingerprint(
    *,
    supplier_product_id: int,
    price: Decimal | None,
    available: bool | None,
    stock: int | None,
    delivery_days: int | None,
    seller: str | None,
    adapter_schema_version: str,
    currency: str | None = None,
) -> str:
    """Return a stable SHA-256 fingerprint from normalized business facts.

    This module is intentionally infrastructure-free. It may be imported by
    local browser agents without configuring SQLAlchemy or DATABASE_URL.
    """
    payload = {
        "supplier_product_id": supplier_product_id,
        "price": format(price, "f") if price is not None else None,
        "currency": currency.strip().upper() if currency else None,
        "available": available,
        "stock": stock,
        "delivery_days": delivery_days,
        "seller": " ".join((seller or "").split()).casefold() or None,
        "adapter_schema_version": adapter_schema_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
