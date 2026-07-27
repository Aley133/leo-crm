from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select

from backend.app.kaspi_credentials_crypto import decrypt_api_token, encrypt_api_token
from backend.app.kaspi_credentials_models import KaspiAccountCredential
from backend.app.main import app
from backend.app.models import MarketplaceAccount, MarketplaceProvider
from backend.app.workspace_models import Workspace


def test_kaspi_api_token_is_encrypted_at_rest(monkeypatch) -> None:
    monkeypatch.setenv("KASPI_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii"))

    encrypted = encrypt_api_token("secret-kaspi-token")

    assert encrypted != "secret-kaspi-token"
    assert "secret-kaspi-token" not in encrypted
    assert decrypt_api_token(encrypted) == "secret-kaspi-token"


def test_credentials_are_owned_by_separate_workspaces(db_session, monkeypatch) -> None:
    monkeypatch.setenv("KASPI_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii"))
    first_workspace = Workspace(name="First owner", slug="first-credential-owner")
    second_workspace = Workspace(name="Second owner", slug="second-credential-owner")
    db_session.add_all([first_workspace, second_workspace])
    db_session.flush()

    first_account = MarketplaceAccount(
        workspace_id=first_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="same-partner",
        display_name="First shop",
        timezone="Asia/Almaty",
    )
    second_account = MarketplaceAccount(
        workspace_id=second_workspace.id,
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="same-partner",
        display_name="Second shop",
        timezone="Asia/Almaty",
    )
    db_session.add_all([first_account, second_account])
    db_session.flush()

    first_credential = KaspiAccountCredential(
        workspace_id=first_workspace.id,
        marketplace_account_id=first_account.id,
        partner_id="same-partner",
        api_token_encrypted=encrypt_api_token("first-token"),
    )
    second_credential = KaspiAccountCredential(
        workspace_id=second_workspace.id,
        marketplace_account_id=second_account.id,
        partner_id="same-partner",
        api_token_encrypted=encrypt_api_token("second-token"),
    )
    db_session.add_all([first_credential, second_credential])
    db_session.commit()

    stored_first = db_session.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == first_workspace.id
        )
    )
    stored_second = db_session.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == second_workspace.id
        )
    )

    assert stored_first is not None
    assert stored_second is not None
    assert decrypt_api_token(stored_first.api_token_encrypted) == "first-token"
    assert decrypt_api_token(stored_second.api_token_encrypted) == "second-token"
    assert stored_first.marketplace_account_id != stored_second.marketplace_account_id


def test_workspace_kaspi_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/kaspi" in paths
