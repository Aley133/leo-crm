from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_postgres_pool_supports_parallel_crm_reads() -> None:
    source = (ROOT / "backend" / "app" / "db.py").read_text(encoding="utf-8")

    assert '"DATABASE_POOL_SIZE", 5' in source
    assert '"DATABASE_MAX_OVERFLOW", 5' in source
    assert '"DATABASE_POOL_TIMEOUT", 10' in source
    assert '"pool_use_lifo": True' in source
    assert "def _bounded_env_int" in source
    assert "finally:\n        db.close()" in source


def test_liveness_does_not_acquire_database_connection() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    health_block = source.split('@app.get("/health")', 1)[1].split('@app.get("/ready")', 1)[0]

    assert "engine.connect" not in health_block
    assert '"database": "not_checked"' in health_block


def test_readiness_checks_database_without_crashing_process() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    ready_block = source.split('@app.get("/ready")', 1)[1]

    assert "with engine.connect() as connection" in ready_block
    assert 'connection.execute(text("SELECT 1"))' in ready_block
    assert "except SQLAlchemyError" in ready_block
    assert "status_code=503" in ready_block
    assert '"database": "unavailable"' in ready_block
    assert '"database": "connected"' in ready_block
