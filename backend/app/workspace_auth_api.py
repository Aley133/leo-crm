from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .workspace_auth import (
    WorkspacePrincipal,
    hash_password,
    issue_session,
    normalize_username,
    require_workspace_principal,
    revoke_session,
    validate_password,
    verify_password,
)
from .workspace_models import AppUser, Workspace

router = APIRouter(prefix="/api/auth", tags=["workspace-auth"])
_bearer = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    workspace_name: str | None = Field(default=None, min_length=1, max_length=255)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str
    workspace_id: int
    workspace_slug: str


class LogoutResponse(BaseModel):
    success: bool = True


def _slug(username: str) -> str:
    return f"{username[:40]}-{secrets.token_hex(4)}"


def _response(raw_token: str, user: AppUser, workspace: Workspace) -> AuthResponse:
    return AuthResponse(
        access_token=raw_token,
        user_id=user.id,
        username=user.username,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    try:
        username = normalize_username(payload.username)
        validate_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if db.scalar(select(AppUser.id).where(AppUser.username == username)) is not None:
        raise HTTPException(status_code=409, detail="Такой логин уже зарегистрирован")

    workspace = Workspace(
        name=(payload.workspace_name or username).strip(),
        slug=_slug(username),
    )
    user = AppUser(
        workspace=workspace,
        username=username,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
        raw_token, _session = issue_session(db, user)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Логин уже занят") from exc
    return _response(raw_token, user, workspace)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    try:
        username = normalize_username(payload.username)
    except ValueError:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль") from None

    user = db.scalar(select(AppUser).where(AppUser.username == username))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    workspace = db.get(Workspace, user.workspace_id)
    if workspace is None or not workspace.is_active:
        raise HTTPException(status_code=403, detail="Рабочее пространство отключено")

    raw_token, _session = issue_session(db, user)
    db.commit()
    return _response(raw_token, user, workspace)


@router.get("/me")
def me(principal: WorkspacePrincipal = Depends(require_workspace_principal)) -> dict:
    return {
        "user_id": principal.user_id,
        "username": principal.username,
        "workspace_id": principal.workspace_id,
        "workspace_slug": principal.workspace_slug,
    }


@router.post("/logout", response_model=LogoutResponse, status_code=status.HTTP_200_OK)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    if credentials is not None:
        revoke_session(db, credentials.credentials)
        db.commit()
    return LogoutResponse()
