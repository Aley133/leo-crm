from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_competitor_worker_is_independent_from_browser_agent() -> None:
    worker = (ROOT / "backend" / "app" / "dumping_competitor_worker.py").read_text(encoding="utf-8")
    runner = (ROOT / "backend" / "app" / "dumping_runner.py").read_text(encoding="utf-8")

    assert "from .browser_agent" not in worker
    assert "import browser_agent" not in worker
    assert "browser_agent_jobs" not in runner
    assert "enqueue_competitor_scan" in runner
    assert "asyncio.run" not in runner
    assert "refresh_dumping_for_supplier_product" in runner


def test_manual_dumping_run_is_queued_instead_of_scanned_inline() -> None:
    api = (ROOT / "backend" / "app" / "dumping_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "backend" / "app" / "static" / "dumping.js").read_text(encoding="utf-8")

    assert 'status_code=status.HTTP_202_ACCEPTED' in api
    assert "enqueue_competitor_scan" in api
    assert "execute_dumping_for_product" not in api
    assert "Поставить в очередь" in frontend
    assert "result.decision" not in frontend


def test_worker_throttles_and_retries_http_429() -> None:
    worker = (ROOT / "backend" / "app" / "dumping_competitor_worker.py").read_text(encoding="utf-8")

    assert "MIN_REQUEST_INTERVAL_SECONDS" in worker
    assert "PERIODIC_REFRESH_SECONDS = 10 * 60" in worker
    assert 'exc.response.status_code == 429' in worker
    assert 'status="retry_wait"' in worker
    assert "Retry-After" in worker
    assert "call_soon_threadsafe" in worker
