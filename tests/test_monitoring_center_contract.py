from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitoring_center_api_is_registered_and_protected() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    assert "from .monitoring_center_api import router as monitoring_center_router" in main
    assert "app.include_router(monitoring_center_router)" in main
    assert 'prefix="/api/monitoring-center"' in source
    assert "dependencies=[Depends(require_service_token)]" in source
    for route in ('@router.get("/summary"', '@router.get("/jobs"', '@router.get("/attempts"', '@router.get("/sources"'):
        assert route in source
    assert "@router.post" not in source


def test_monitoring_center_uses_only_real_runtime_entities() -> None:
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    for entity in ("MonitorTarget", "MonitorAttempt", "BrowserAgentJob", "SourceHealth"):
        assert entity in source
    for fictional_metric in ("cpu_percent", "memory_mb", "heartbeat"):
        assert fictional_metric not in source


def test_monitoring_center_page_is_live_and_read_only() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "monitoring.js").read_text(encoding="utf-8")

    assert '@router.get("/crm/monitoring"' in ui
    assert 'FileResponse(STATIC_DIR / "monitoring.html")' in ui
    for element_id in (
        'id="leased-body"',
        'id="jobs-body"',
        'id="attempts-body"',
        'id="sources-body"',
        'id="job-status"',
        'id="job-source"',
        'id="attempt-source"',
        'id="attempt-period"',
        'id="only-errors"',
    ):
        assert element_id in html
    assert '"leo_crm_service_token"' in script
    for endpoint in (
        "/api/monitoring-center/summary",
        "/api/monitoring-center/jobs",
        "/api/monitoring-center/attempts",
        "/api/monitoring-center/sources",
    ):
        assert endpoint in script
    for method in ('method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert method not in script


def test_monitoring_center_formats_runtime_data_for_operators() -> None:
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "monitoring.js").read_text(encoding="utf-8")

    for metric_id in (
        'id="jobs-leased"',
        'id="attempts-success-24h"',
        'id="attempts-failed-24h"',
        'id="attempts-average"',
        'id="last-attempt"',
    ):
        assert metric_id in html
    assert "const formatDuration" in script
    assert "const errorCell" in script
    assert '<details class="error-details">' in script
    assert "const renderLeased" in script
    assert "24 * 60 * 60 * 1000" in script
    assert "cachedJobs" in script
    assert "cachedAttempts" in script
