from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_postgres_pool_is_bounded_and_fails_fast_under_pressure() -> None:
    source = (ROOT / "backend" / "app" / "db.py").read_text(encoding="utf-8")

    assert '"DB_POOL_SIZE"' in source
    assert "default=5" in source
    assert '"DB_MAX_OVERFLOW"' in source
    assert "default=2" in source
    assert '"DB_POOL_TIMEOUT_SECONDS"' in source
    assert '"pool_use_lifo": True' in source
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


def test_dumping_page_serializes_database_backed_reads() -> None:
    source = (ROOT / "backend" / "app" / "static" / "dumping.js").read_text(
        encoding="utf-8"
    )

    load_page = source.split("const loadPage = async", 1)[1].split(
        "const pollDumpingRuntime", 1
    )[0]
    assert "databaseReadInFlight" in load_page
    assert 'await request("/api/dumping")' in load_page
    assert 'await request("/api/dumping/feed-status")' in load_page
    assert 'await request("/api/dumping/runtime")' in load_page
    assert "Promise.all" not in load_page


def test_liveness_does_not_acquire_database_connection() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    health_block = source.split('@app.get("/health")', 1)[1].split('@app.get("/ready")', 1)[0]

    assert "engine.connect" not in health_block
    assert '"database": "not_checked"' in health_block
    assert '"memory_rss_mb": _process_rss_mb()' in health_block
    assert '"database_pool": _database_pool_snapshot()' in health_block


def test_pool_timeout_is_returned_as_retryable_overload() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "@app.exception_handler(SQLAlchemyTimeoutError)" in source
    assert 'status_code=503' in source
    assert 'headers={"Retry-After": "2"}' in source
    assert '"error_code": "database_pool_busy"' in source


def test_api_thread_pool_is_bounded_for_render_memory_limit() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert 'os.getenv("API_THREAD_LIMIT"' in source
    assert "requested = int(raw) if raw else 12" in source
    assert "current_default_thread_limiter().total_tokens = limit" in source
    assert "app.state.thread_pool_limit = _configure_thread_pool()" in source


def test_readiness_checks_database_without_crashing_process() -> None:
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    ready_block = source.split('@app.get("/ready")', 1)[1]

    assert "with engine.connect() as connection" in ready_block
    assert 'connection.execute(text("SELECT 1"))' in ready_block
    assert "except SQLAlchemyError" in ready_block
    assert "status_code=503" in ready_block
    assert '"database": "unavailable"' in ready_block
    assert '"database": "ok"' in ready_block
