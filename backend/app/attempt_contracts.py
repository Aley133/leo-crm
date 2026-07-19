from __future__ import annotations

from enum import StrEnum


class AttemptOutcome(StrEnum):
    """Pure monitoring outcome contract shared by server and remote agents."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"
