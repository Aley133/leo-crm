import os
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")

    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)

    return value


def _engine_options(database_url: str) -> dict[str, Any]:
    """Return safe engine settings for the selected database dialect.

    PostgreSQL keeps the deliberately small pool approved for the Supabase
    connection budget. Application code must avoid opening more concurrent
    request sessions than this contract permits.
    """

    url = make_url(database_url)
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    if url.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        if url.database in {None, "", ":memory:"}:
            options["poolclass"] = StaticPool
        return options

    options.update(
        {
            "pool_size": 2,
            "max_overflow": 1,
            "pool_timeout": 10,
        }
    )
    return options


DATABASE_URL = _database_url()
engine = create_engine(DATABASE_URL, **_engine_options(DATABASE_URL))

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
