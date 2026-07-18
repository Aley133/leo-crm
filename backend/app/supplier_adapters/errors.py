from __future__ import annotations

from dataclasses import dataclass

from backend.app.monitoring import AttemptOutcome


@dataclass(eq=False)
class AdapterError(Exception):
    """Typed supplier-adapter failure safe for scheduler classification."""

    message: str
    outcome: AttemptOutcome
    error_code: str
    http_status: int | None = None

    def __str__(self) -> str:
        return self.message


class AdapterTimeoutError(AdapterError):
    def __init__(self, message: str = "Supplier request timed out") -> None:
        super().__init__(message, AttemptOutcome.TIMEOUT, "adapter_timeout")


class AdapterNetworkError(AdapterError):
    def __init__(self, message: str = "Supplier network request failed") -> None:
        super().__init__(message, AttemptOutcome.NETWORK_ERROR, "adapter_network_error")


class AdapterRateLimitedError(AdapterError):
    def __init__(self, message: str = "Supplier rate limit reached", *, http_status: int = 429) -> None:
        super().__init__(message, AttemptOutcome.RATE_LIMITED, "adapter_rate_limited", http_status)


class AdapterCaptchaError(AdapterError):
    def __init__(self, message: str = "Supplier returned a captcha page", *, http_status: int | None = None) -> None:
        super().__init__(message, AttemptOutcome.CAPTCHA, "adapter_captcha", http_status)


class AdapterBlockedError(AdapterError):
    def __init__(self, message: str = "Supplier blocked access", *, http_status: int | None = None) -> None:
        super().__init__(message, AttemptOutcome.BLOCKED, "adapter_blocked", http_status)


class AdapterAuthRequiredError(AdapterError):
    def __init__(self, message: str = "Supplier requires authentication", *, http_status: int | None = None) -> None:
        super().__init__(message, AttemptOutcome.AUTH_REQUIRED, "adapter_auth_required", http_status)


class AdapterNotFoundError(AdapterError):
    def __init__(self, message: str = "Supplier product was not found", *, http_status: int = 404) -> None:
        super().__init__(message, AttemptOutcome.NOT_FOUND, "adapter_not_found", http_status)


class AdapterParseError(AdapterError):
    def __init__(self, message: str = "Supplier response could not be normalized", *, http_status: int | None = None) -> None:
        super().__init__(message, AttemptOutcome.PARSE_ERROR, "adapter_parse_error", http_status)
