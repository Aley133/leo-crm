from __future__ import annotations

from collections import deque
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


@dataclass(frozen=True)
class BrowserAgentEvent:
    agent_id: str
    event: str
    status: str
    current_job_id: int | None
    occurred_at: datetime
    detail: str | None = None


_lock = Lock()
_agents: dict[str, BrowserAgentPresence] = {}
_events: dict[str, deque[BrowserAgentEvent]] = {}
_ONLINE_WINDOW = timedelta(seconds=30)
_MAX_EVENTS_PER_AGENT = 200


def record_browser_agent_heartbeat(
    *,
    agent_id: str,
    status: str,
    version: str | None = None,
    current_job_id: int | None = None,
) -> BrowserAgentPresence:
    now = utc_now()
    with _lock:
        previous = _agents.get(agent_id)
        presence = BrowserAgentPresence(
            agent_id=agent_id,
            status=status,
            version=version or (previous.version if previous else None),
            current_job_id=current_job_id,
            last_seen_at=now,
        )
        _agents[agent_id] = presence

        changed = (
            previous is None
            or previous.status != presence.status
            or previous.current_job_id != presence.current_job_id
            or previous.version != presence.version
        )
        if changed:
            queue = _events.setdefault(agent_id, deque(maxlen=_MAX_EVENTS_PER_AGENT))
            queue.append(
                BrowserAgentEvent(
                    agent_id=agent_id,
                    event="connected" if previous is None else "state_changed",
                    status=presence.status,
                    current_job_id=presence.current_job_id,
                    occurred_at=now,
                    detail=(
                        f"version={presence.version or 'unknown'}"
                        if previous is None
                        else f"{previous.status} -> {presence.status}"
                    ),
                )
            )
    return presence


def list_online_browser_agents() -> list[BrowserAgentPresence]:
    cutoff = utc_now() - _ONLINE_WINDOW
    with _lock:
        stale = [agent_id for agent_id, item in _agents.items() if item.last_seen_at < cutoff]
        for agent_id in stale:
            previous = _agents.pop(agent_id)
            queue = _events.setdefault(agent_id, deque(maxlen=_MAX_EVENTS_PER_AGENT))
            queue.append(
                BrowserAgentEvent(
                    agent_id=agent_id,
                    event="offline",
                    status="offline",
                    current_job_id=previous.current_job_id,
                    occurred_at=utc_now(),
                    detail="heartbeat timeout",
                )
            )
        return sorted(_agents.values(), key=lambda item: item.agent_id)


def list_browser_agent_events(agent_id: str, *, limit: int = 100) -> list[BrowserAgentEvent]:
    safe_limit = max(1, min(limit, _MAX_EVENTS_PER_AGENT))
    with _lock:
        items = list(_events.get(agent_id, ()))
    return list(reversed(items[-safe_limit:]))
