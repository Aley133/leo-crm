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
from backend.app.models import MarketplaceAccount, Product
from backend.app.workspace_models import Workspace


LEGACY_TEST_WORKSPACE_ID = 1


def _default_legacy_workspace(
    _target,
    _args: tuple,
    kwargs: dict,
) -> None:
    """Keep pre-multitenant test constructors valid only inside tests."""
    kwargs.setdefault("workspace_id", LEGACY_TEST_WORKSPACE_ID)


for model in (MarketplaceAccount, Product):
    event.listen(model, "init", _default_legacy_workspace, propagate=True)


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
