from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy import func, select

from backend.app.commerce.repository import _latest_order_raw_payloads
from backend.app.kaspi_raw_receiver_jobs import _history_record
from backend.app.marketplace_import import prune_order_raw_payload_history
from backend.app.models import MarketplaceAccount, MarketplaceRawPayload


def _account(db_session, external_id: str) -> MarketplaceAccount:
    account = MarketplaceAccount(
        provider="kaspi",
        external_account_id=external_id,
        display_name=external_id,
        timezone="Asia/Almaty",
    )
    db_session.add(account)
    db_session.flush()
    return account


def _raw_payload(
    db_session,
    *,
    account_id: int,
    order_id: str,
    content_hash: str,
    received_at: datetime,
    marker: str,
    delivery_cost: float = 0,
) -> None:
    db_session.add(
        MarketplaceRawPayload(
            marketplace_account_id=account_id,
            payload_type="order",
            external_object_id=order_id,
            content_hash=content_hash,
            payload_json={
                "attributes": {
                    "marker": marker,
                    "deliveryCostForSeller": delivery_cost,
                }
            },
            received_at=received_at,
        )
    )


def test_orders_page_deserializes_only_latest_snapshot_per_account(
    db_session,
) -> None:
    first = _account(db_session, "partner-1")
    second = _account(db_session, "partner-2")
    started_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    for index in range(40):
        _raw_payload(
            db_session,
            account_id=first.id,
            order_id="same-order",
            content_hash=f"first-{index}",
            received_at=started_at + timedelta(minutes=index),
            marker=f"first-{index}",
        )
        _raw_payload(
            db_session,
            account_id=second.id,
            order_id="same-order",
            content_hash=f"second-{index}",
            received_at=started_at + timedelta(minutes=index),
            marker=f"second-{index}",
        )
    db_session.commit()

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture_statement)
    try:
        payloads = _latest_order_raw_payloads(
            db_session,
            order_keys={(first.id, "same-order"), (second.id, "same-order")},
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_statement)

    assert len(payloads) == 2
    assert payloads[(first.id, "same-order")]["attributes"]["marker"] == "first-39"
    assert payloads[(second.id, "same-order")]["attributes"]["marker"] == "second-39"
    assert any("row_number()" in statement.casefold() for statement in statements)


def test_delivery_transition_reads_scalar_history_instead_of_full_json(
    db_session,
) -> None:
    account = _account(db_session, "partner-history")
    started_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    for index, cost in enumerate((0, 0, 1507, 1507)):
        _raw_payload(
            db_session,
            account_id=account.id,
            order_id="order-history",
            content_hash=f"history-{index}",
            received_at=started_at + timedelta(minutes=index),
            marker=f"history-{index}",
            delivery_cost=cost,
        )
    db_session.commit()

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture_statement)
    try:
        history = _history_record(
            db_session,
            marketplace_account_id=account.id,
            external_order_id="order-history",
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_statement)

    assert history is not None
    assert history["transfer_started_at"].startswith("2026-07-30T12:02:00")
    assert history["transfer_started_source"] == "delivery_cost_transition"
    assert any("json_extract" in statement.casefold() for statement in statements)
    assert all(
        "select marketplace_raw_payloads.payload_json," not in statement.casefold()
        for statement in statements
    )


def test_order_raw_payload_history_is_bounded_per_order(db_session) -> None:
    account = _account(db_session, "partner-retention")
    started_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    for index in range(25):
        _raw_payload(
            db_session,
            account_id=account.id,
            order_id="bounded-order",
            content_hash=f"retention-{index}",
            received_at=started_at + timedelta(minutes=index),
            marker=f"retention-{index}",
        )
    db_session.flush()

    removed = prune_order_raw_payload_history(
        db_session,
        marketplace_account_id=account.id,
        external_order_id="bounded-order",
    )
    remaining = db_session.scalar(
        select(func.count(MarketplaceRawPayload.id)).where(
            MarketplaceRawPayload.marketplace_account_id == account.id,
            MarketplaceRawPayload.external_object_id == "bounded-order",
        )
    )

    assert removed == 5
    assert remaining == 20
