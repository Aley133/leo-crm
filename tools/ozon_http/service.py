from __future__ import annotations

import time
from typing import Any

from .client import OzonHttpClient
from .config import Config
from .matcher import rank
from .session_client import OzonSessionHttpClient
from .session_profile import CurlProfile


def _rank_and_time(result: dict[str, Any], query: str | None, started: float) -> dict[str, Any]:
    items = list(result.get("items") or [])
    if query:
        items = rank(query, items)
    result["items"] = items
    result["best_match"] = items[0] if items else None
    result["elapsed_total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["read_only"] = True
    result["playwright_used"] = False
    result["browser_used"] = False
    return result


def search_ozon(query: str, page: int = 1, strategy: str = "auto") -> dict[str, Any]:
    started = time.perf_counter()
    client = OzonHttpClient(Config.load())
    try:
        result = client.search(query=query, page=page, strategy=strategy)
        return _rank_and_time(result, query, started)
    finally:
        client.close()


def replay_session(profile: CurlProfile) -> dict[str, Any]:
    started = time.perf_counter()
    client = OzonSessionHttpClient(profile, Config.load())
    try:
        result = client.replay_exact()
        return _rank_and_time(result, None, started)
    finally:
        client.close()


def search_with_session(profile: CurlProfile, query: str, page: int = 1) -> dict[str, Any]:
    started = time.perf_counter()
    client = OzonSessionHttpClient(profile, Config.load())
    try:
        result = client.search(query=query, page=page)
        return _rank_and_time(result, query, started)
    finally:
        client.close()
