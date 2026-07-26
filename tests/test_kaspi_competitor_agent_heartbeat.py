from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_competitor_agent_exposes_isolated_heartbeat_contract() -> None:
    api = (ROOT / "backend" / "app" / "kaspi_competitor_agent_api.py").read_text(encoding="utf-8")
    agent = (ROOT / "tools" / "kaspi_competitor_agent.py").read_text(encoding="utf-8")

    assert '@router.post("/heartbeat")' in api
    assert '@router.get("/agents/status")' in api
    assert "AGENT_ONLINE_SECONDS" in api
    assert "/api/kaspi-competitor-agent/heartbeat" in agent
    assert "HEARTBEAT_SECONDS" in agent
    assert "tools.browser_agent" not in agent
    assert "playwright" not in agent.lower()


def test_dumping_page_shows_local_competitor_agent_status() -> None:
    page = (ROOT / "backend" / "app" / "static" / "dumping.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "dumping-agent-status.js").read_text(encoding="utf-8")

    assert 'id="agent-source"' in page
    assert "dumping-agent-status.js" in page
    assert "/api/kaspi-competitor-agent/agents/status" in script
    assert "Агент ещё не подключался" in script
    assert "Онлайн" in script
