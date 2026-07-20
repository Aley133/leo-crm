from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .auth import require_service_token
from .browser_agent_presence import list_browser_agent_events, list_online_browser_agents


router = APIRouter(
    prefix="/api/browser-agent-registry",
    tags=["browser-agent-registry"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/agents")
def list_agents() -> list[dict]:
    return [
        {
            "agent_id": item.agent_id,
            "status": item.status,
            "version": item.version,
            "current_job_id": item.current_job_id,
            "last_seen_at": item.last_seen_at,
        }
        for item in list_online_browser_agents()
    ]


@router.get("/agents/{agent_id}/events")
def list_agent_events(agent_id: str, limit: int = Query(default=100, ge=1, le=200)) -> list[dict]:
    return [
        {
            "agent_id": item.agent_id,
            "event": item.event,
            "status": item.status,
            "current_job_id": item.current_job_id,
            "occurred_at": item.occurred_at,
            "detail": item.detail,
        }
        for item in list_browser_agent_events(agent_id, limit=limit)
    ]
