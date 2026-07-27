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


def _default_legacy_workspace_for_old_account_factories(
    _target: MarketplaceAccount,
    _args: tuple,
    kwargs: dict,
) -> None:
    """Keep legacy MarketplaceAccount test constructors valid during M2.

    Several regression tests create their own SQLAlchemy sessions instead of using
    the shared ``db_session`` fixture. A mapper init hook is therefore required so
    every test-only MarketplaceAccount constructor receives workspace 1 unless the
    test explicitly supplies another workspace.

    This hook lives only in tests. Production remains fail-closed because the model
    and database column are still NOT NULL and production constructors receive no
    implicit workspace.
    """
    kwargs.setdefault("workspace_id", LEGACY_TEST_WORKSPACE_ID)


# Install once at test import time so it also covers tests with private sessions.
event.listen(
    MarketplaceAccount,
    "init",
    _default_legacy_workspace_for_old_account_factories,
    propagate=True,
)


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
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
