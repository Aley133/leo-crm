from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event
from sqlalchemy.orm import Session

from .dumping_runner import refresh_dumping_for_supplier_product
from .workspace_context import current_workspace_id


_PENDING_KEY = "dumping_refresh_supplier_product_ids"
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dumping-refresh")


def mark_supplier_product_for_dumping_refresh(session: Session, supplier_product_id: int) -> None:
    pending = session.info.setdefault(_PENDING_KEY, set())
    pending.add((current_workspace_id(), int(supplier_product_id)))


@event.listens_for(Session, "after_commit")
def _run_dumping_after_commit(session: Session) -> None:
    pending = tuple(session.info.pop(_PENDING_KEY, set()))
    for workspace_id, supplier_product_id in pending:
        _EXECUTOR.submit(
            refresh_dumping_for_supplier_product,
            supplier_product_id,
            workspace_id=workspace_id,
        )


@event.listens_for(Session, "after_rollback")
def _discard_dumping_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)
