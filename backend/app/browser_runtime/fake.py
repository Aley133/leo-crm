from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .base import BrowserRequest, BrowserResponse, BrowserRuntimeError


class FakeBrowserRuntime:
    """Deterministic runtime for supplier-adapter tests.

    Outcomes are consumed in order. Each outcome is either a BrowserResponse or
    BrowserRuntimeError. The fake performs no I/O and never persists business data.
    """

    runtime_id = "fake"

    def __init__(
        self,
        outcomes: Iterable[BrowserResponse | BrowserRuntimeError],
    ) -> None:
        self._outcomes = deque(outcomes)
        self.requests: list[BrowserRequest] = []

    async def execute(self, request: BrowserRequest) -> BrowserResponse:
        self.requests.append(request)
        if not self._outcomes:
            raise RuntimeError("FakeBrowserRuntime has no configured outcome")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, BrowserRuntimeError):
            raise outcome
        return outcome
