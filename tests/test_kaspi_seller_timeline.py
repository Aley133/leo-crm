from backend.app.kaspi_seller.timeline import derive_timeline_transition


def _snapshot(stage: str | None, *, state: str = "STATE", status: str = "STATUS") -> dict:
    return {
        "stage": stage,
        "state": state,
        "status": status,
        "preorder": False,
    }


def test_first_snapshot_creates_business_event() -> None:
    event = derive_timeline_transition(
        previous_snapshot=None,
        current_snapshot=_snapshot("HANDOVER"),
    )

    assert event is not None
    assert event.event_type == "ORDER_ASSEMBLED"
    assert event.from_stage is None
    assert event.to_stage == "HANDOVER"


def test_stage_change_creates_transfer_event() -> None:
    event = derive_timeline_transition(
        previous_snapshot=_snapshot("HANDOVER"),
        current_snapshot=_snapshot(
            "SHIPPING",
            state="KASPI_DELIVERY_TRANSMITTED",
            status="TRANSMITTED",
        ),
    )

    assert event is not None
    assert event.event_type == "ORDER_TRANSFERRED"
    assert event.from_stage == "HANDOVER"
    assert event.to_stage == "SHIPPING"
    assert event.payload["state"] == "KASPI_DELIVERY_TRANSMITTED"


def test_unchanged_stage_does_not_pollute_timeline() -> None:
    event = derive_timeline_transition(
        previous_snapshot=_snapshot("SHIPPING", state="OLD"),
        current_snapshot=_snapshot("SHIPPING", state="NEW"),
    )

    assert event is None


def test_terminal_stages_have_explicit_event_types() -> None:
    expected = {
        "DELIVERED": "ORDER_DELIVERED",
        "RETURNED": "ORDER_RETURNED",
        "CANCELLED": "ORDER_CANCELLED",
    }
    for stage, event_type in expected.items():
        event = derive_timeline_transition(
            previous_snapshot=_snapshot("SHIPPING"),
            current_snapshot=_snapshot(stage),
        )
        assert event is not None
        assert event.event_type == event_type
