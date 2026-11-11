"""Bounded workspace snapshots; concurrent page refreshes share one DB read."""
from collections import OrderedDict
from threading import Lock
from time import monotonic

from fastapi import HTTPException

_GUARD = Lock()
_ENTRIES = OrderedDict()
MAX_ENTRIES = 16
TTL_SECONDS = 5
STALE_SECONDS = 30


def inventory_snapshot(db, workspace, build, *, fresh=False):
    key = (db.get_bind(), workspace)
    with _GUARD:
        entry = _ENTRIES.get(key)
        if entry is None:
            # Never evict an entry whose refresh is still running.
            for old_key, old in list(_ENTRIES.items()):
                if len(_ENTRIES) < MAX_ENTRIES:
                    break
                if not old["lock"].locked():
                    _ENTRIES.pop(old_key)
            if len(_ENTRIES) >= MAX_ENTRIES:
                raise HTTPException(503, "Список обновляется. Повторите через несколько секунд.", headers={"Retry-After": "3"})
            entry = {"lock": Lock(), "value": None, "at": 0}
            _ENTRIES[key] = entry
        _ENTRIES.move_to_end(key)
        value, age = entry["value"], monotonic() - entry["at"]
        if value is not None and age < TTL_SECONDS and not fresh:
            return value
        refreshing = entry["lock"].acquire(blocking=False)
    if not refreshing:
        if value is not None and age < STALE_SECONDS and not fresh:
            return {**value, "snapshot_stale": True,
                "warnings": [*value.get("warnings", []), "Список обновляется; показан предыдущий снимок."]}
        raise HTTPException(503, "Список уже обновляется. Повторите через несколько секунд.", headers={"Retry-After": "3"})
    try:
        result = build()
        with _GUARD:
            entry.update(value=result, at=monotonic())
        return result
    finally:
        entry["lock"].release()
