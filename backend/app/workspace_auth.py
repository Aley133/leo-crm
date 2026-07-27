from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .workspace_models import AppUser, UserSession, Workspace

_PASSWORD_ITERATIONS = 310_000
_SESSION_DAYS = 30
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class WorkspacePrincipal:
    user_id: int
    username: str
    workspace_id: int
    workspace_slug: str


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not 3 <= len(username) <= 64:
        raise ValueError("Логин должен содержать от 3 до 64 символов")
    if not all(char.isalnum() or char in {"_", "-", "."} for char in username):
        raise ValueError("Логин может содержать буквы, цифры, точку, дефис и подчёркивание")
    return username


def validate_password(value: str) -> None:
    if len(value) < 8:
        raise ValueError("Пароль должен содержать минимум 8 символов")
    if len(value) > 256:
        raise ValueError("Пароль слишком длинный")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_text),
        )
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def issue_session(db: Session, user: AppUser) -> tuple[str, UserSession]:
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=_SESSION_DAYS),
    )
    db.add(session)
    db.flush()
    return raw_token, session


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def require_workspace_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    db: Session = Depends(get_db),
) -> WorkspacePrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    now = datetime.now(UTC)
    row = db.execute(
        select(UserSession, AppUser, Workspace)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .join(Workspace, Workspace.id == AppUser.workspace_id)
        .where(
            UserSession.token_hash == _token_hash(credentials.credentials),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            AppUser.is_active.is_(True),
            Workspace.is_active.is_(True),
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _session, user, workspace = row
    return WorkspacePrincipal(
        user_id=user.id,
        username=user.username,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
    )


def revoke_session(db: Session, raw_token: str) -> bool:
    session = db.scalar(
        select(UserSession).where(
            UserSession.token_hash == _token_hash(raw_token),
            UserSession.revoked_at.is_(None),
        )
    )
    if session is None:
        return False
    session.revoked_at = datetime.now(UTC)
    db.flush()
    return True
