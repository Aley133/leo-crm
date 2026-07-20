from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_models import BrowserAgent
from .browser_agent_presence import list_browser_agent_events
from .db import get_db
from .lease_engine import utc_now


router = APIRouter(
    prefix="/api/browser-agent-registry",
    tags=["browser-agent-registry"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/agents")
def list_agents(db: Session = Depends(get_db)) -> list[dict]:
    cutoff = utc_now() - timedelta(seconds=30)
    agents = db.scalars(select(BrowserAgent).order_by(BrowserAgent.agent_id)).all()
    return [
        {
            "agent_id": item.agent_id,
            "hostname": item.hostname,
            "platform": item.platform,
            "status": item.status if item.last_seen_at >= cutoff else "offline",
            "version": item.version,
            "current_job_id": item.current_job_id,
            "last_seen_at": item.last_seen_at,
            "leases_taken": item.leases_taken,
            "leases_succeeded": item.leases_succeeded,
            "leases_failed": item.leases_failed,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in agents
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
