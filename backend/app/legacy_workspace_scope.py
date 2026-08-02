from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from .db import Base
from .workspace_context import current_workspace_id


class WorkspaceIsolationError(RuntimeError):
    pass


def _workspace_models() -> tuple[type, ...]:
    return tuple(
        mapper.class_
        for mapper in Base.registry.mappers
        if hasattr(mapper.class_, "workspace_id")
    )


def _unscoped(session: Session, execution_options=None) -> bool:
    if session.info.get("include_all_workspaces"):
        return True
    return bool(
        execution_options
        and execution_options.get("include_all_workspaces")
    )


@event.listens_for(Session, "do_orm_execute")
def _scope_workspace_queries(execute_state) -> None:
    if _unscoped(execute_state.session, execute_state.execution_options):
        return

    workspace_id = current_workspace_id()
    if execute_state.is_select:
        statement = execute_state.statement
        for model in _workspace_models():
            statement = statement.options(
                with_loader_criteria(
                    model,
                    model.workspace_id == workspace_id,
                    include_aliases=True,
                )
            )
        execute_state.statement = statement
        return

    if execute_state.is_update or execute_state.is_delete:
        table = getattr(execute_state.statement, "table", None)
        if table is not None and "workspace_id" in table.c:
            execute_state.statement = execute_state.statement.where(
                table.c.workspace_id == workspace_id
            )


@event.listens_for(Session, "before_flush")
def _guard_workspace_writes(session: Session, _flush_context, _instances) -> None:
    if _unscoped(session):
        return
    workspace_id = current_workspace_id()
    for item in (*session.new, *session.dirty, *session.deleted):
        if not hasattr(type(item), "workspace_id"):
            continue
        owned_by = getattr(item, "workspace_id", None)
        if owned_by is None:
            setattr(item, "workspace_id", workspace_id)
            continue
        if int(owned_by) != workspace_id:
            raise WorkspaceIsolationError(
                f"{type(item).__name__} belongs to workspace {owned_by}, "
                f"current workspace is {workspace_id}"
            )
