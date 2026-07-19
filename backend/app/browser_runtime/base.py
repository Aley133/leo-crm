from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlparse


class BrowserFailureCode(StrEnum):
    TIMEOUT = "timeout"
    NAVIGATION_FAILED = "navigation_failed"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    SESSION_EXPIRED = "session_expired"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"
    INVALID_RESPONSE = "invalid_response"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    url: str
    operation: str
    timeout_seconds: float = 30.0
    session_key: str | None = None
    wait_for: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        if not self.operation.strip():
            raise ValueError("operation must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.session_key is not None and not self.session_key.strip():
            raise ValueError("session_key must not be blank")


@dataclass(frozen=True, slots=True)
class BrowserResponse:
    final_url: str
    content: str
    observed_at: datetime
    duration_ms: int
    runtime_id: str
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parsed = urlparse(self.final_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("final_url must be an absolute HTTP(S) URL")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if not self.runtime_id.strip():
            raise ValueError("runtime_id must not be empty")


class BrowserRuntimeError(RuntimeError):
    def __init__(
        self,
        code: BrowserFailureCode | str,
        message: str,
        *,
        retryable: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = BrowserFailureCode(code)
        self.retryable = retryable
        self.metadata = metadata or {}


class BrowserRuntime(Protocol):
    runtime_id: str

    async def execute(self, request: BrowserRequest) -> BrowserResponse:
        """Execute one bounded browser operation without business persistence."""
