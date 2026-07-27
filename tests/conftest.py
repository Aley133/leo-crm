from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app import models, monitoring, suppliers, workspace_models  # noqa: F401,E402
from backend.app.models import MarketplaceAccount, Product
from backend.app.workspace_models import Workspace


LEGACY_TEST_WORKSPACE_ID = 1


def _default_legacy_workspace_for_old_account_factories(
    _target: MarketplaceAccount,
    _args: tuple,
    kwargs: dict,
) -> None:
    """Keep legacy MarketplaceAccount test constructors valid during M2."""
    kwargs.setdefault("workspace_id", LEGACY_TEST_WORKSPACE_ID)


def _default_legacy_workspace_for_old_product_factories(
    _target: Product,
    _args: tuple,
    kwargs: dict,
) -> None:
    """Keep legacy Product test constructors valid during M6."""
    kwargs.setdefault("workspace_id", LEGACY_TEST_WORKSPACE_ID)


def _seed_legacy_workspace_for_every_test_transaction(
    _session: Session,
    _transaction,
    connection,
) -> None:
    """Ensure workspace 1 exists in SQLite and PostgreSQL test databases.

    Some concurrency tests create their own engines and sessions instead of using
    ``db_session``. Product and MarketplaceAccount now have strict workspace foreign
    keys, so every independent test transaction must have the legacy owner row.
    This hook exists only in tests and keeps production fail-closed.
    """
    values = {
        "id": LEGACY_TEST_WORKSPACE_ID,
        "name": "Legacy test workspace",
        "slug": "legacy-test",
        "is_active": True,
    }
    if connection.dialect.name == "postgresql":
        statement = postgresql_insert(Workspace.__table__).values(**values).on_conflict_do_nothing(
            index_elements=[Workspace.__table__.c.id]
        )
    else:
        statement = sqlite_insert(Workspace.__table__).values(**values).on_conflict_do_nothing(
            index_elements=[Workspace.__table__.c.id]
        )
    connection.execute(statement)


# Install once at test import time so hooks cover tests with private sessions.
event.listen(
    MarketplaceAccount,
    "init",
    _default_legacy_workspace_for_old_account_factories,
    propagate=True,
)
event.listen(
    Product,
    "init",
    _default_legacy_workspace_for_old_product_factories,
    propagate=True,
)
event.listen(
    Session,
    "after_begin",
    _seed_legacy_workspace_for_every_test_transaction,
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
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
