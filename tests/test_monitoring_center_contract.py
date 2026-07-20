from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitoring_center_api_is_registered_and_protected() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    assert "from .monitoring_center_api import router as monitoring_center_router" in main
    assert "app.include_router(monitoring_center_router)" in main
    assert 'prefix="/api/monitoring-center"' in source
    assert "dependencies=[Depends(require_service_token)]" in source
    for route in (
        '@router.get("/summary"',
        '@router.get("/jobs"',
        '@router.get("/jobs/{job_id}"',
        '@router.get("/jobs/{job_id}/events"',
        '@router.post("/jobs/{job_id}/retry"',
        '@router.post("/jobs/{job_id}/cancel"',
        '@router.get("/attempts"',
        '@router.get("/sources"',
    ):
        assert route in source


def test_monitoring_center_operator_actions_preserve_lease_safety() -> None:
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    assert "Only completed jobs can be retried" in source
    assert "Only queued jobs can be cancelled" in source
    assert 'error_code = "operator_cancelled"' in source
    assert "with_for_update()" in source
    assert "BrowserAgentJobStatus.LEASED.value" in source


def test_monitoring_center_uses_only_real_runtime_entities() -> None:
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    for entity in ("MonitorTarget", "MonitorAttempt", "BrowserAgentJob", "SourceHealth"):
        assert entity in source
    for fictional_metric in ("cpu_percent", "memory_mb", "heartbeat"):
        assert fictional_metric not in source


def test_monitoring_center_exposes_runtime_lifecycle_without_new_schema() -> None:
    source = (ROOT / "backend" / "app" / "monitoring_center_api.py").read_text(encoding="utf-8")

    assert "def _job_lifecycle" in source
    for state in (
        '"waiting_for_agent"',
        '"processing"',
        '"lease_expired"',
        '"finished"',
        '"cancelled"',
        '"failed"',
    ):
        assert state in source
    assert "lifecycle_state" in source
    assert "wait_reason" in source
    assert "Job добавлен в очередь Browser Agent" in source
    assert "Browser Agent выполняет навигацию, парсинг и сохранение результата" in source


def test_monitoring_center_page_is_live_and_operable() -> None:
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")
    html = (ROOT / "backend" / "app" / "static" / "monitoring.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "monitoring.js").read_text(encoding="utf-8")

    assert '@router.get("/crm/monitoring"' in ui
    assert 'FileResponse(STATIC_DIR / "monitoring.html")' in ui
    for element_id in (
        'id="runtime-health"',
        'id="runtime-health-title"',
        'id="runtime-health-detail"',
        'id="agent-queue-count"',
        'id="agent-last-work"',
        'id="agent-average-time"',
        'id="leased-body"',
        'id="jobs-body"',
        'id="attempts-body"',
        'id="sources-body"',
        'id="job-status"',
        'id="job-source"',
        'id="attempt-source"',
        'id="attempt-period"',
        'id="only-errors"',
        'id="job-dialog"',
        'id="job-dialog-content"',
    ):
        assert element_id in html
    assert "<th>Этап</th>" in html
    assert '"leo_crm_service_token"' in script
    for endpoint in (
        "/api/monitoring-center/summary",
        "/api/monitoring-center/jobs",
        "/api/monitoring-center/attempts",
        "/api/monitoring-center/sources",
        "/events",
    ):
        assert endpoint in script
    assert 'data-action="retry"' in script
    assert 'data-action="cancel"' in script
    assert "const mutateJob" in script
    assert '/${action}`' in script
    assert 'method:"POST"' in script
    assert 'method:"PUT"' not in script
    assert 'method:"PATCH"' not in script
    assert 'method:"DELETE"' not in script


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
    assert "const lifecycleCell" in script
    assert "const renderRuntimeHealth" in script
    assert "const recentAttemptStats" in script
    assert "cachedSources" in script
    assert "Runtime работает нормально" in script
    assert "Ожидает Browser Agent" in script
    assert "Ошибка сети" in script
    assert '<details class="error-details">' in script
    assert "const renderLeased" in script
    assert "24*60*60*1000" in script
    assert "cachedJobs" in script
    assert "cachedAttempts" in script
    assert "const actionButtons" in script
    assert "const inspectJob" in script
    assert "Причина ожидания" in script
    assert "Хронология Job" in script
    assert "Сервис Render временно недоступен или перезапускается." in script
