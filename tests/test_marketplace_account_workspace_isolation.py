from sqlalchemy import select

from backend.app.models import MarketplaceAccount, MarketplaceProvider
from backend.app.workspace_models import Workspace


def test_same_kaspi_partner_id_isolated_between_workspaces(db_session) -> None:
    first_workspace = Workspace(name="First", slug="first-marketplace")
    second_workspace = Workspace(name="Second", slug="second-marketplace")
    db_session.add_all([first_workspace, second_workspace])
    db_session.flush()

    first_account = MarketplaceAccount(
        workspace_id=first_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-100",
        display_name="First shop",
        timezone="Asia/Almaty",
    )
    second_account = MarketplaceAccount(
        workspace_id=second_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-100",
        display_name="Second shop",
        timezone="Asia/Almaty",
    )
    db_session.add_all([first_account, second_account])
    db_session.commit()

    assert first_account.id != second_account.id
    assert db_session.scalar(
        select(MarketplaceAccount).where(
            MarketplaceAccount.workspace_id == first_workspace.id,
            MarketplaceAccount.external_account_id == "partner-100",
        )
    ) is first_account
    assert db_session.scalar(
        select(MarketplaceAccount).where(
            MarketplaceAccount.workspace_id == second_workspace.id,
            MarketplaceAccount.external_account_id == "partner-100",
        )
    ) is second_account


def test_marketplace_account_workspace_is_required() -> None:
    assert MarketplaceAccount.__table__.c.workspace_id.nullable is False
    constraint_names = {
        constraint.name for constraint in MarketplaceAccount.__table__.constraints
    }
    assert "uq_marketplace_account_workspace_provider_external" in constraint_names
