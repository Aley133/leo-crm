from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_selected_browser_queue_is_atomic_and_deduplicated() -> None:
    script = (ROOT / "backend" / "app" / "browser_agent_dispatch.py").read_text(encoding="utf-8")

    assert "def queue_browser_target_now(" in script
    assert ".with_for_update(of=MonitorTarget)" in script
    assert "BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES)" in script
    assert "BrowserQueueFailure.ALREADY_PENDING" in script
    assert "session.flush()" in script
    assert "session.commit()" not in script


def test_manual_queue_endpoint_uses_dispatcher_and_never_runs_browser() -> None:
    script = (ROOT / "backend" / "app" / "browser_agent_monitoring_api.py").read_text(
        encoding="utf-8"
    )

    assert 'router.post("/{target_id}/queue-browser-agent"' in script
    assert "queue_browser_target_now(" in script
    assert "OzonBrowserAccessAdapter" not in script
    assert "Playwright" not in script
    assert "process_claimed_target" not in script


def test_manual_queue_does_not_change_scheduler_cadence() -> None:
    script = (ROOT / "backend" / "app" / "browser_agent_dispatch.py").read_text(encoding="utf-8")
    function = script.split("def queue_browser_target_now(", 1)[1].split(
        "def dispatch_due_browser_targets(", 1
    )[0]

    assert "next_check_at" not in function
    assert "last_checked_at" not in function
    assert "interval_seconds" not in function
