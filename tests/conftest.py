from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app import models, monitoring, suppliers, workspace_models  # noqa: F401,E402
from backend.app.models import MarketplaceAccount
from backend.app.workspace_models import Workspace


LEGACY_TEST_WORKSPACE_ID = 1


def _attach_legacy_workspace_to_old_fixtures(
    session: Session,
    _flush_context,
    _instances,
) -> None:
    """Keep pre-workspace test factories valid during the staged migration.

    Production code remains fail-closed: MarketplaceAccount.workspace_id is still
    NOT NULL and new request paths must pass an explicit workspace. This adapter
    exists only in the isolated SQLite test session so the existing regression
    suite continues to exercise FIFO, orders, purchases and sync unchanged.
    """
    for item in session.new:
        if isinstance(item, MarketplaceAccount) and item.workspace_id is None:
            item.workspace_id = LEGACY_TEST_WORKSPACE_ID


@pytest.fixture()
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    event.listen(session, "before_flush", _attach_legacy_workspace_to_old_fixtures)
    session.add(
        Workspace(
            id=LEGACY_TEST_WORKSPACE_ID,
            name="Legacy test workspace",
            slug="legacy-test",
        )
    )
    session.commit()
    try:
        yield session
    finally:
        event.remove(session, "before_flush", _attach_legacy_workspace_to_old_fixtures)
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
