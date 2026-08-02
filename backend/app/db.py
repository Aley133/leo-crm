import os
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


def _bounded_int_setting(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")

    if value.startswith("postgres://"):
        value = value.replace("postgres://", "postgresql://", 1)

    return value


def _engine_options(database_url: str) -> dict[str, Any]:
    """Return safe engine settings for the selected database dialect.

    PostgreSQL uses a bounded pool sized for the API, two local agents and one
    background order synchronizer. A short timeout prevents a traffic burst
    from retaining dozens of blocked request threads until Render exhausts its
    memory limit.
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
            "pool_size": _bounded_int_setting(
                "DB_POOL_SIZE",
                default=5,
                minimum=1,
                maximum=10,
            ),
            "max_overflow": _bounded_int_setting(
                "DB_MAX_OVERFLOW",
                default=2,
                minimum=0,
                maximum=5,
            ),
            "pool_timeout": _bounded_int_setting(
                "DB_POOL_TIMEOUT_SECONDS",
                default=2,
                minimum=1,
                maximum=10,
            ),
            "pool_use_lifo": True,
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


def get_unscoped_db():
    """Yield a session for cross-workspace infrastructure workers only."""
    db = SessionLocal()
    db.info["include_all_workspaces"] = True
    try:
        yield db
    finally:
        db.close()
