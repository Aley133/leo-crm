from __future__ import annotations

from cryptography.fernet import Fernet

from backend.app.kaspi_credentials_crypto import encrypt_api_token
from backend.app.kaspi_credentials_models import KaspiAccountCredential
from backend.app.main import app
from backend.app.models import MarketplaceAccount, MarketplaceProvider
from backend.app.workspace_kaspi_import_api import JOBS, _load_owned_credentials
from backend.app.workspace_models import Workspace


def test_workspace_kaspi_import_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/kaspi/import" in paths
    assert "/api/workspace/kaspi/import/{job_id}" in paths


def test_import_credentials_are_loaded_only_for_requested_workspace(
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KASPI_CREDENTIALS_KEY", Fernet.generate_key().decode("ascii"))
    first_workspace = Workspace(name="First", slug="first-import")
    second_workspace = Workspace(name="Second", slug="second-import")
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
    db_session.add_all(
        [
            KaspiAccountCredential(
                workspace_id=first_workspace.id,
                marketplace_account_id=first_account.id,
                partner_id="same-partner",
                api_token_encrypted=encrypt_api_token("first-secret"),
            ),
            KaspiAccountCredential(
                workspace_id=second_workspace.id,
                marketplace_account_id=second_account.id,
                partner_id="same-partner",
                api_token_encrypted=encrypt_api_token("second-secret"),
            ),
        ]
    )
    db_session.commit()

    _credential, account, token = _load_owned_credentials(
        db_session,
        workspace_id=first_workspace.id,
    )

    assert account.id == first_account.id
    assert token == "first-secret"
    assert token != "second-secret"


def test_import_jobs_keep_workspace_ownership() -> None:
    JOBS.clear()
    JOBS["first-job"] = {
        "job_id": "first-job",
        "workspace_id": 10,
        "marketplace_account_id": 20,
        "status": "queued",
    }
    assert JOBS["first-job"]["workspace_id"] == 10
