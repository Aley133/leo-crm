from backend.app.marketplace_import import normalize_kaspi_order


def _payload(*, status: str | None, state: str | None = "KASPI_DELIVERY") -> dict:
    attributes = {
        "code": "1000025629",
        "revision": f"rev-{status}-{state}",
        "currency": "KZT",
        "totalPrice": "9999",
        "creationDate": "2026-07-18T10:00:00Z",
        "entries": [],
    }
    if status is not None:
        attributes["status"] = status
    if state is not None:
        attributes["state"] = state
    return {"id": "order-1", "attributes": attributes}


def test_lifecycle_status_wins_over_kaspi_delivery_state() -> None:
    completed = normalize_kaspi_order(_payload(status="COMPLETED"))
    cancelled = normalize_kaspi_order(_payload(status="CANCELLED"))
    returned = normalize_kaspi_order(
        _payload(status="KASPI_DELIVERY_RETURN_REQUESTED")
    )

    assert completed.status == "delivered"
    assert completed.original_status == "COMPLETED"
    assert cancelled.status == "cancelled"
    assert returned.status == "returned"


def test_legacy_delivery_alias_remains_supported() -> None:
    order = normalize_kaspi_order(_payload(status="DELIVERED", state=None))

    assert order.status == "delivered"
    assert order.original_status == "DELIVERED"


def test_state_is_only_a_safe_fallback_when_status_is_missing() -> None:
    order = normalize_kaspi_order(_payload(status=None, state="KASPI_DELIVERY"))

    assert order.status == "accepted"
    assert order.original_status == "KASPI_DELIVERY"
