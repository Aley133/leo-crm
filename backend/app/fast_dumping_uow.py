"""Reuse already-loaded ORM rows inside one Fast API operation only."""
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session


_KEY = "fast_dumping_unit_of_work"


@dataclass
class FastUnitOfWork:
    workspace_id: int
    refs: set = field(default_factory=set)
    locked_states: dict = field(default_factory=dict)


@contextmanager
def fast_unit_of_work(db: Session, workspace_id: int):
    previous = db.info.get(_KEY)
    unit = FastUnitOfWork(workspace_id, set(db.identity_map.values()))
    db.info[_KEY] = unit
    try:
        yield unit
    finally:
        if previous is None:
            db.info.pop(_KEY, None)
        else:
            db.info[_KEY] = previous


@event.listens_for(Session, "loaded_as_persistent")
@event.listens_for(Session, "pending_to_persistent")
def _retain_row(db, instance):
    unit = db.info.get(_KEY)
    if unit is not None:
        unit.refs.add(instance)


def current_fast_unit(db: Session) -> FastUnitOfWork | None:
    return db.info.get(_KEY)


def locked_state(db: Session, workspace_id: int, product_id: int):
    unit = current_fast_unit(db)
    if unit is None or unit.workspace_id != workspace_id:
        return None
    saved = unit.locked_states.get(product_id)
    if (saved is not None and saved[0] is not None
            and saved[0] is db.get_transaction() and inspect(saved[1]).persistent):
        return saved[1]
    return None


def remember_locked_state(db: Session, state):
    unit = current_fast_unit(db)
    if unit is not None and state is not None and state.workspace_id == unit.workspace_id:
        unit.locked_states[state.product_id] = (db.get_transaction(), state)


def workspace_row(db: Session, model, row_id: int, workspace_id: int):
    row = db.get(model, row_id)
    return row if row is not None and row.workspace_id == workspace_id else None
