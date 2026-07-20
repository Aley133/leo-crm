from __future__ import annotations

from backend.app.browser_agent_presence import (
    list_browser_agent_events,
    record_browser_agent_heartbeat,
)
from backend.app.main import app


def test_browser_agent_registry_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/browser-agent-registry/agents" in paths
    assert "/api/browser-agent-registry/agents/{agent_id}/events" in paths


def test_browser_agent_registry_records_state_changes_only() -> None:
    agent_id = "contract-agent-registry"
    record_browser_agent_heartbeat(
        agent_id=agent_id,
        status="idle",
        version="0.1.0",
    )
    record_browser_agent_heartbeat(
        agent_id=agent_id,
        status="idle",
        version="0.1.0",
    )
    record_browser_agent_heartbeat(
        agent_id=agent_id,
        status="running",
        version="0.1.0",
        current_job_id=17,
    )

    events = list_browser_agent_events(agent_id)
    assert [item.event for item in events[:2]] == ["state_changed", "connected"]
    assert events[0].status == "running"
    assert events[0].current_job_id == 17
    assert events[1].status == "idle"
