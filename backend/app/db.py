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

    url = make_url(value)
    transaction_pooler_enabled = os.getenv(
        "SUPABASE_TRANSACTION_POOLER_ENABLED",
        "true",
    ).strip().casefold() not in {"0", "false", "no", "off"}
    is_supabase_session_pooler = (
        url.get_backend_name() == "postgresql"
        and str(url.host or "").casefold().endswith(".pooler.supabase.com")
        and url.port == 5432
    )
    if transaction_pooler_enabled and is_supabase_session_pooler:
        # Session mode dedicates one scarce Postgres backend to every long-lived
        # Render connection. Transaction mode shares those backends and remains
        # compatible with this application: all ORM state is transaction-local
        # and psycopg2 does not auto-prepare statements. Require encrypted
        # transport and disable GSS negotiation explicitly for Supavisor.
        query = dict(url.query)
        query.setdefault("sslmode", "require")
        query.setdefault("gssencmode", "disable")
        url = url.set(port=6543).set(query=query)
        return url.render_as_string(hide_password=False)

    return value


def _engine_options(database_url: str) -> dict[str, Any]:
    """Return safe engine settings for the selected database dialect.

    PostgreSQL uses a bounded pool sized for the API, two isolated local agents
    and background order synchronization. Overflow is disabled by default so a
    request burst cannot consume every Supabase session-pool slot. A short
    timeout lets short agent bursts queue locally, then returns retryable
    overload instead of accumulating blocked threads indefinitely.
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
                minimum=3,
                maximum=10,
            ),
            "max_overflow": _bounded_int_setting(
                "DB_MAX_OVERFLOW",
                default=0,
                minimum=0,
                maximum=5,
            ),
            "pool_timeout": _bounded_int_setting(
                "DB_POOL_TIMEOUT_SECONDS",
                default=5,
                minimum=1,
                maximum=10,
            ),
            "connect_args": {
                "connect_timeout": _bounded_int_setting(
                    "DB_CONNECT_TIMEOUT_SECONDS",
                    default=8,
                    minimum=3,
                    maximum=30,
                )
            },
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
