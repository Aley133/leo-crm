from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import require_service_token
from .browser_agent_dispatch import (
    BrowserQueueFailure,
    queue_browser_target_now,
)
from .browser_agent_models import BrowserAgentJob
from .db import get_db


router = APIRouter(
    prefix="/api/monitor-targets",
    tags=["browser-agent"],
    dependencies=[Depends(require_service_token)],
)


@router.post("/{target_id}/queue-browser-agent", status_code=status.HTTP_201_CREATED)
def queue_monitor_target_for_browser_agent(target_id: int, db: Session = Depends(get_db)):
    """Queue a selected target for execution by a local Browser Agent.

    This endpoint never opens a browser on the API server and never changes the
    target's cadence. Concurrent calls are serialized by the dispatcher contract.
    """
    try:
        result = queue_browser_target_now(db, target_id=target_id, supplier_code="ozon")
        if result.failure is BrowserQueueFailure.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Monitor target or supplier product not found")
        if result.failure is BrowserQueueFailure.NOT_ACTIVE:
            raise HTTPException(status_code=409, detail="Monitor target must be active")
        if result.failure is BrowserQueueFailure.UNSUPPORTED_SUPPLIER:
            raise HTTPException(
                status_code=409,
                detail="Browser agent currently supports supplier code 'ozon'",
            )

        reused = result.failure is BrowserQueueFailure.ALREADY_PENDING
        job = db.get(BrowserAgentJob, result.job_id)
        if job is None:
            raise RuntimeError("queued browser agent job was not persisted")
        db.commit()
        return {"job_id": job.id, "status": job.status, "reused": reused}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise