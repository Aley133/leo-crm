import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.app.db import _engine_options  # noqa: E402


def test_sqlite_memory_uses_static_pool_without_queue_pool_arguments() -> None:
    options = _engine_options("sqlite+pysqlite:///:memory:")

    assert options["poolclass"] is StaticPool
    assert options["connect_args"] == {"check_same_thread": False}
    assert "pool_size" not in options
    assert "max_overflow" not in options
    assert "pool_timeout" not in options


def test_postgresql_uses_explicit_small_connection_pool() -> None:
    options = _engine_options("postgresql://user:pass@example.test:5432/leo")

    assert options["pool_size"] == 2
    assert options["max_overflow"] == 1
    assert options["pool_timeout"] == 10
