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


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _engine_options(database_url: str) -> dict[str, Any]:
    """Return safe engine settings for the selected database dialect.

    The CRM dashboard performs several independent read requests in parallel.
    PostgreSQL therefore needs enough pooled connections for one page load plus
    health/readiness traffic. Values remain configurable so the deployment can
    stay within the Supabase connection budget.
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
            "pool_size": _bounded_env_int(
                "DATABASE_POOL_SIZE", 5, minimum=1, maximum=20
            ),
            "max_overflow": _bounded_env_int(
                "DATABASE_MAX_OVERFLOW", 5, minimum=0, maximum=20
            ),
            "pool_timeout": _bounded_env_int(
                "DATABASE_POOL_TIMEOUT", 10, minimum=1, maximum=60
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
