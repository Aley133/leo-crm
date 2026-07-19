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


@pytest.mark.parametrize(
    ("page_size", "max_pages"),
    [(0, 1), (101, 1), (10, 0), (10, 101)],
)
def test_full_sync_rejects_unbounded_inputs(page_size: int, max_pages: int) -> None:
    with pytest.raises(ValueError):
        sync_kaspi_orders(
            lambda: None,
            StubTransport(),
            marketplace_account_id=1,
            page_size=page_size,
            max_pages=max_pages,
        )
