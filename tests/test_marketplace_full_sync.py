from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from backend.app import marketplace_full_sync
from backend.app.marketplace_full_sync import sync_kaspi_orders
from backend.app.marketplace_sync import MarketplaceSyncResult


@dataclass
class StubTransport:
    pass


def test_full_sync_aggregates_pages_until_completion(monkeypatch) -> None:
    results = iter(
        [
            MarketplaceSyncResult(uuid4(), 10, 8, 2, "2"),
            MarketplaceSyncResult(uuid4(), 6, 5, 1, None),
        ]
    )
    calls: list[tuple[int, int]] = []

    def fake_page(session_factory, transport, *, marketplace_account_id: int, limit: int):
        calls.append((marketplace_account_id, limit))
        return next(results)

    monkeypatch.setattr(marketplace_full_sync, "sync_kaspi_order_page", fake_page)

    result = sync_kaspi_orders(
        lambda: None,
        StubTransport(),
        marketplace_account_id=7,
        page_size=10,
        max_pages=5,
    )

    assert result.pages_processed == 2
    assert result.fetched_count == 16
    assert result.imported_count == 13
    assert result.updated_count == 3
    assert result.next_cursor is None
    assert result.completed is True
    assert result.stopped_reason is None
    assert calls == [(7, 10), (7, 10)]


def test_full_sync_stops_at_safety_cap(monkeypatch) -> None:
    def fake_page(session_factory, transport, *, marketplace_account_id: int, limit: int):
        return MarketplaceSyncResult(uuid4(), limit, limit, 0, "next")

    monkeypatch.setattr(marketplace_full_sync, "sync_kaspi_order_page", fake_page)

    result = sync_kaspi_orders(
        lambda: None,
        StubTransport(),
        marketplace_account_id=1,
        page_size=20,
        max_pages=3,
    )

    assert result.pages_processed == 3
    assert result.fetched_count == 60
    assert result.completed is False
    assert result.next_cursor == "next"
    assert result.stopped_reason == "page_limit_reached"


def test_full_sync_stops_between_pages_when_time_budget_is_exhausted(monkeypatch) -> None:
    calls = 0

    def fake_page(session_factory, transport, *, marketplace_account_id: int, limit: int):
        nonlocal calls
        calls += 1
        return MarketplaceSyncResult(uuid4(), limit, limit, 0, "next")

    clock = iter((0.0, 30.0))
    monkeypatch.setattr(marketplace_full_sync, "sync_kaspi_order_page", fake_page)
    monkeypatch.setattr(marketplace_full_sync, "monotonic", lambda: next(clock))

    result = sync_kaspi_orders(
        lambda: None,
        StubTransport(),
        marketplace_account_id=1,
        page_size=10,
        max_pages=20,
        max_duration_seconds=25,
    )

    assert calls == 1
    assert result.pages_processed == 1
    assert result.fetched_count == 10
    assert result.completed is False
    assert result.next_cursor == "next"
    assert result.stopped_reason == "time_budget_exhausted"


@pytest.mark.parametrize(
    ("page_size", "max_pages", "max_duration_seconds"),
    [
        (0, 1, 25),
        (101, 1, 25),
        (10, 0, 25),
        (10, 101, 25),
        (10, 1, 4),
        (10, 1, 121),
    ],
)
def test_full_sync_rejects_unbounded_inputs(
    page_size: int,
    max_pages: int,
    max_duration_seconds: int,
) -> None:
    with pytest.raises(ValueError):
        sync_kaspi_orders(
            lambda: None,
            StubTransport(),
            marketplace_account_id=1,
            page_size=page_size,
            max_pages=max_pages,
            max_duration_seconds=max_duration_seconds,
        )
