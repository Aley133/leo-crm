from __future__ import annotations

from sqlalchemy import Column, Integer, event, select
from sqlalchemy.orm import Session, with_loader_criteria

from .models import (
    MarketplaceAccount,
    MarketplaceImportCheckpoint,
    MarketplaceImportExecution,
    MarketplaceOrder,
    MarketplaceRawPayload,
    Product,
)

LEGACY_WORKSPACE_ID = 1


# The production database already contains workspace columns from the aborted
# multi-account migration, while the restored BARWORK ORM intentionally matches
# the last stable pre-workspace code. Register the existing database columns in
# SQLAlchemy metadata without changing the mapped legacy models or UI.
for _model in (MarketplaceAccount, Product):
    if "workspace_id" not in _model.__table__.c:
        _model.__table__.append_column(Column("workspace_id", Integer, nullable=False))


def _legacy_account_ids():
    return select(MarketplaceAccount.id).where(
        MarketplaceAccount.__table__.c.workspace_id == LEGACY_WORKSPACE_ID
    )


@event.listens_for(Session, "do_orm_execute")
def _scope_restored_barwork_queries(execute_state) -> None:
    """Keep the restored single-shop CRM isolated to BARWORK (workspace 1).

    This is a compatibility boundary only. It does not alter the restored
    business logic, routes, HTML or JavaScript. A query may opt out explicitly
    with ``execution_options(include_all_workspaces=True)`` for maintenance.
    """

    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("include_all_workspaces"):
        return

    account_ids = _legacy_account_ids()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            MarketplaceAccount,
            lambda cls: cls.__table__.c.workspace_id == LEGACY_WORKSPACE_ID,
            include_aliases=False,
        ),
        with_loader_criteria(
            Product,
            lambda cls: cls.__table__.c.workspace_id == LEGACY_WORKSPACE_ID,
            include_aliases=False,
        ),
        with_loader_criteria(
            MarketplaceOrder,
            lambda cls: cls.marketplace_account_id.in_(account_ids),
            include_aliases=False,
        ),
        with_loader_criteria(
            MarketplaceRawPayload,
            lambda cls: cls.marketplace_account_id.in_(account_ids),
            include_aliases=False,
        ),
        with_loader_criteria(
            MarketplaceImportExecution,
            lambda cls: cls.marketplace_account_id.in_(account_ids),
            include_aliases=False,
        ),
        with_loader_criteria(
            MarketplaceImportCheckpoint,
            lambda cls: cls.marketplace_account_id.in_(account_ids),
            include_aliases=False,
        ),
    )
