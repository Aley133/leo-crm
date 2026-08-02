from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


LEGACY_WORKSPACE_ID = 1
WORKSPACE_HEADER = "X-Workspace-ID"

_workspace_id: ContextVar[int] = ContextVar(
    "leo_crm_workspace_id",
    default=LEGACY_WORKSPACE_ID,
)


def current_workspace_id() -> int:
    return int(_workspace_id.get())


def set_current_workspace_id(workspace_id: int) -> Token[int]:
    normalized = int(workspace_id)
    if normalized < 1:
        raise ValueError("workspace_id must be positive")
    return _workspace_id.set(normalized)


def reset_current_workspace_id(token: Token[int]) -> None:
    _workspace_id.reset(token)


@contextmanager
def workspace_context(workspace_id: int) -> Iterator[int]:
    token = set_current_workspace_id(workspace_id)
    try:
        yield int(workspace_id)
    finally:
        reset_current_workspace_id(token)


class WorkspaceOwned:
    """Add explicit tenant ownership to an operational ORM model.

    The database foreign keys are installed by Alembic. Keeping the mixin free
    of an ORM-level relationship lets lightweight SQLite tests create only the
    tables they import while production still enforces ownership in Postgres.
    """

    @declared_attr
    def workspace_id(cls) -> Mapped[int]:
        return mapped_column(
            Integer,
            nullable=False,
            default=current_workspace_id,
            server_default=str(LEGACY_WORKSPACE_ID),
            index=True,
        )
