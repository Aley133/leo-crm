from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_postgres_pool_keeps_approved_small_connection_budget() -> None:
    source = (ROOT / "backend" / "app" / "db.py").read_text(encoding="utf-8")

    assert '"pool_size": 2' in source
    assert '"max_overflow": 1' in source
    assert '"pool_timeout": 10' in source
    assert "finally:\n        db.close()" in source


def test_monitoring_page_serializes_database_backed_reads() -> None:
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(
        encoding="utf-8"
    )
    queue_script = (
        ROOT / "backend" / "app" / "static" / "monitoring-request-queue.js"
    ).read_text(encoding="utf-8")

    queue_position = html.index('/static/monitoring-request-queue.js')
    monitoring_position = html.index('/static/monitoring.js')
    assert queue_position < monitoring_position
    assert 'url.startsWith("/api/monitoring-center/")' in queue_script
    assert 'method === "GET"' in queue_script
    assert "monitoringReadQueue.then" in queue_script
    assert "originalFetch(input, init)" in queue_script


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
