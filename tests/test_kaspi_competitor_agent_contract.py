from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_competitor_agent_is_isolated_from_supplier_browser_agent() -> None:
    source = (ROOT / "tools" / "kaspi_competitor_agent.py").read_text(encoding="utf-8")

    assert "scan_kaspi_competitors" in source
    assert "/api/kaspi-competitor-agent/claim" in source
    assert "/api/kaspi-competitor-agent/jobs/" in source
    assert "tools.browser_agent" not in source
    assert "Playwright" not in source
    assert "CHROME_CDP_ENDPOINT" not in source


def test_render_competitor_worker_is_a_noop_and_queues_local_jobs() -> None:
    source = (ROOT / "backend" / "app" / "dumping_competitor_worker.py").read_text(encoding="utf-8")

    assert "queue_competitor_job" in source
    assert "Server worker intentionally disabled" in source
    assert "execute_dumping_for_product" not in source
    assert "httpx" not in source
    assert "browser_agent_jobs" not in source


def test_local_competitor_agent_api_is_registered() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "kaspi_competitor_agent_api.py").read_text(encoding="utf-8")

    assert "kaspi_competitor_agent_router" in main
    assert "app.include_router(kaspi_competitor_agent_router)" in main
    assert 'prefix="/api/kaspi-competitor-agent"' in api
    assert '@router.post("/claim")' in api
    assert '@router.post("/jobs/{job_id}/complete")' in api
