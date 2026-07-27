from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from backend.app.main import app
from backend.app.workspace_auth import hash_password, issue_session, verify_password
from backend.app.workspace_models import AppUser, UserSession, Workspace


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("strong-password")
    second = hash_password("strong-password")

    assert first != second
    assert verify_password("strong-password", first)
    assert not verify_password("wrong-password", first)


def test_users_belong_to_separate_workspaces(db_session) -> None:
    first_workspace = Workspace(name="First", slug="first")
    second_workspace = Workspace(name="Second", slug="second")
    first_user = AppUser(
        workspace=first_workspace,
        username="first-owner",
        password_hash=hash_password("password-one"),
    )
    second_user = AppUser(
        workspace=second_workspace,
        username="second-owner",
        password_hash=hash_password("password-two"),
    )
    db_session.add_all([first_user, second_user])
    db_session.commit()

    assert first_user.workspace_id != second_user.workspace_id
    assert db_session.scalar(
        select(AppUser).where(
            AppUser.workspace_id == first_workspace.id,
            AppUser.username == "second-owner",
        )
    ) is None


def test_sessions_store_only_token_hash(db_session) -> None:
    workspace = Workspace(name="Owner", slug="owner")
    user = AppUser(
        workspace=workspace,
        username="owner",
        password_hash=hash_password("owner-password"),
    )
    db_session.add(user)
    db_session.flush()

    raw_token, session = issue_session(db_session, user)
    db_session.commit()

    assert raw_token != session.token_hash
    assert len(session.token_hash) == 64
    assert session.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)
    assert db_session.scalar(select(UserSession).where(UserSession.user_id == user.id)) is not None


def test_auth_routes_are_registered_without_replacing_service_token_routes() -> None:
    paths = {route.path for route in app.routes}

    assert "/api/auth/register" in paths
    assert "/api/auth/login" in paths
    assert "/api/auth/me" in paths
    assert "/api/auth/logout" in paths
    assert "/api/products" in paths
    assert "/api/browser-agent/heartbeat" in paths
