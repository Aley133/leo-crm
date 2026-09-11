import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.app.db import _database_url, _engine_options  # noqa: E402


def test_sqlite_memory_uses_static_pool_without_queue_pool_arguments() -> None:
    options = _engine_options("sqlite+pysqlite:///:memory:")

    assert options["poolclass"] is StaticPool
    assert options["connect_args"] == {"check_same_thread": False}
    assert "pool_size" not in options
    assert "max_overflow" not in options
    assert "pool_timeout" not in options


def test_postgresql_uses_bounded_runtime_connection_pool(monkeypatch) -> None:
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("DB_MAX_OVERFLOW", raising=False)
    monkeypatch.delenv("DB_POOL_TIMEOUT_SECONDS", raising=False)
    options = _engine_options("postgresql://user:pass@example.test:5432/leo")

    assert options["pool_size"] == 5
    assert options["max_overflow"] == 0
    assert options["pool_timeout"] == 5
    assert options["connect_args"] == {"connect_timeout": 8}
    assert options["pool_use_lifo"] is True


def test_postgresql_pool_settings_are_configurable_and_bounded(monkeypatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "99")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "-4")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "invalid")

    options = _engine_options("postgresql://user:pass@example.test:5432/leo")

    assert options["pool_size"] == 10
    assert options["max_overflow"] == 0
    assert options["pool_timeout"] == 5


def test_postgresql_pool_keeps_capacity_for_two_agents_and_order_polling(monkeypatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")

    options = _engine_options("postgresql://user:pass@example.test:5432/leo")

    assert options["pool_size"] == 3
    assert options["max_overflow"] == 1


def test_supabase_session_pooler_is_switched_to_transaction_mode(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_TRANSACTION_POOLER_ENABLED", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.project:p%40ss@aws-1-ap-south-1.pooler.supabase.com:5432/postgres?application_name=leo",
    )

    url = make_url(_database_url())

    assert url.port == 6543
    assert url.password == "p@ss"
    assert url.query["application_name"] == "leo"
    assert url.query["sslmode"] == "require"
    assert url.query["gssencmode"] == "disable"


def test_supabase_transaction_pooler_switch_can_be_disabled(monkeypatch) -> None:
    source = "postgresql://user:pass@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
    monkeypatch.setenv("DATABASE_URL", source)
    monkeypatch.setenv("SUPABASE_TRANSACTION_POOLER_ENABLED", "false")

    assert _database_url() == source


def test_unrelated_postgresql_url_is_not_rewritten(monkeypatch) -> None:
    source = "postgresql://user:pass@example.test:5432/leo"
    monkeypatch.setenv("DATABASE_URL", source)
    monkeypatch.delenv("SUPABASE_TRANSACTION_POOLER_ENABLED", raising=False)

    assert _database_url() == source
