import os

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from backend.app.auth import require_service_token  # noqa: E402


def test_service_token_fails_closed_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVICE_API_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        require_service_token(None)

    assert exc_info.value.status_code == 503


def test_service_token_rejects_missing_or_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", "correct-secret")

    with pytest.raises(HTTPException) as missing:
        require_service_token(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as invalid:
        require_service_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-secret"))
    assert invalid.value.status_code == 401


def test_service_token_accepts_matching_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_API_TOKEN", "correct-secret")

    assert (
        require_service_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="correct-secret")
        )
        is None
    )
