from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from .lease_engine import utc_now


@dataclass(frozen=True)
class BrowserAgentPresence:
    agent_id: str
    status: str
    version: str | None
    current_job_id: int | None
    last_seen_at: datetime


_lock = Lock()
_agents: dict[str, BrowserAgentPresence] = {}
_ONLINE_WINDOW = timedelta(seconds=30)


def record_browser_agent_heartbeat(
    *,
    agent_id: str,
    status: str,
    version: str | None = None,
    current_job_id: int | None = None,
) -> BrowserAgentPresence:
    presence = BrowserAgentPresence(
        agent_id=agent_id,
        status=status,
        version=version,
        current_job_id=current_job_id,
        last_seen_at=utc_now(),
    )
    with _lock:
        _agents[agent_id] = presence
    return presence


def list_online_browser_agents() -> list[BrowserAgentPresence]:
    cutoff = utc_now() - _ONLINE_WINDOW
    with _lock:
        stale = [agent_id for agent_id, item in _agents.items() if item.last_seen_at < cutoff]
        for agent_id in stale:
            _agents.pop(agent_id, None)
        return sorted(_agents.values(), key=lambda item: item.agent_id)
