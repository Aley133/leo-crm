from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy import select, update

from backend.app import (  # noqa: F401
    browser_agent_models,
    dumping_models,
    inventory_models,
    models,
    monitoring,
    pricing_models,
    product_identity_models,
    purchase_models,
    revenue_models,
    suppliers,
)
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.db import Base, get_db
from backend.app.dumping_api import (
    list_dumping_products,
    read_public_kaspi_feed,
    read_workspace_kaspi_feed,
)
from backend.app.dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from backend.app.dumping_runner import apply_competitor_snapshot
from backend.app.inventory_models import InventoryBatch
from backend.app.kaspi_offer_competitor import KaspiCompetitorSnapshot
from backend.app.legacy_workspace_scope import WorkspaceIsolationError
from backend.app.kaspi_credentials_crypto import decrypt_api_token
from backend.app.main import app
from backend.app.models import MarketplaceAccount, MarketplaceOrder, Product
from backend.app.suppliers import Supplier
from backend.app.workspace_context import workspace_context
from backend.app.workspace_kaspi import validate_kaspi_connection
from backend.app.workspace_models import KaspiAccountCredential, Workspace


ROOT = Path(__file__).resolve().parents[1]

OPERATIONAL_TABLES = {
    "marketplace_import_executions",
    "marketplace_import_checkpoints",
    "marketplace_orders",
    "marketplace_order_lines",
    "marketplace_order_events",
    "marketplace_raw_payloads",
    "outbox_events",
    "suppliers",
    "supplier_products",
    "product_bindings",
    "monitor_targets",
    "monitor_attempts",
    "supplier_offer_states",
    "supplier_offer_observations",
    "source_health",
    "inventory_batches",
    "inventory_allocations",
    "dumping_policies",
    "dumping_runs",
    "pricing_policies",
    "price_calculations",
    "marketplace_listings",
    "marketplace_listing_issues",
    "marketplace_listing_events",
    "purchase_requests",
    "purchase_request_lines",
    "purchase_events",
    "purchase_receipts",
    "purchase_receipt_lines",
    "daily_revenue_snapshots",
    "browser_agent_jobs",
}

FAST_DUMPING_OPERATIONAL_TABLES = {
    "fast_dumping_policies",
    "fast_dumping_states",
    "fast_dumping_jobs",
}


def _seed_workspace(db, workspace_id: int, slug: str):
    db.add(Workspace(id=workspace_id, name=slug.upper(), slug=slug, is_active=True))
    db.flush()
    with workspace_context(workspace_id):
        product = Product(
            kaspi_product_id="same-kaspi-id",
            merchant_sku="same-sku",
            name=f"Product {workspace_id}",
            status="active",
        )
        account = MarketplaceAccount(
            provider="kaspi",
            external_account_id=f"partner-{workspace_id}",
            display_name=f"Shop {workspace_id}",
            timezone="Asia/Almaty",
        )
        supplier = Supplier(code="ozon", name=f"Ozon {workspace_id}")
        db.add_all((product, account, supplier))
        db.flush()
        order = MarketplaceOrder(
            marketplace_account_id=account.id,
            external_order_id=f"order-{workspace_id}",
            external_code=f"CODE-{workspace_id}",
            status="new",
            original_status="NEW",
            currency="KZT",
            total_amount=Decimal("1000"),
            ordered_at=datetime.now(UTC),
        )
        inventory = InventoryBatch(
            product_id=product.id,
            received_at=datetime.now(UTC),
            quantity_received=workspace_id,
            quantity_remaining=workspace_id,
            unit_cost=Decimal("500"),
        )
        policy = DumpingPolicy(
            product_id=product.id,
            enabled=True,
            auto_publish_xml=True,
        )
        feed = KaspiXmlFeed(
            merchant_id=f"merchant-{workspace_id}",
            source_xml=f"<catalog>{workspace_id}</catalog>",
            generated_xml=f"<catalog>{workspace_id}</catalog>",
            active=True,
        )
        browser_job = BrowserAgentJob(
            supplier_product_id=workspace_id,
            url=f"https://example.test/{workspace_id}",
            status="queued",
        )
        db.add_all((order, inventory, policy, feed, browser_job))
        db.flush()
        return {
            "product_id": product.id,
            "feed_id": feed.id,
        }


def test_every_operational_model_has_workspace_ownership() -> None:
    mapped_tables = {
        mapper.local_table.name: mapper.local_table
        for mapper in Base.registry.mappers
    }
    operational_tables = OPERATIONAL_TABLES | FAST_DUMPING_OPERATIONAL_TABLES
    assert operational_tables <= mapped_tables.keys()
    assert all("workspace_id" in mapped_tables[name].c for name in operational_tables)


def test_two_workspaces_are_invisible_to_each_other(db_session) -> None:
    first = _seed_workspace(db_session, 1, "barwork")
    second = _seed_workspace(db_session, 2, "second-shop")
    db_session.commit()
    db_session.expunge_all()

    for workspace_id, expected_name, expected_order, expected_quantity in (
        (1, "Product 1", "order-1", 1),
        (2, "Product 2", "order-2", 2),
    ):
        with workspace_context(workspace_id):
            products = list(db_session.scalars(select(Product)).all())
            suppliers = list(db_session.scalars(select(Supplier)).all())
            orders = list(db_session.scalars(select(MarketplaceOrder)).all())
            inventory = list(db_session.scalars(select(InventoryBatch)).all())
            policies = list(db_session.scalars(select(DumpingPolicy)).all())
            feeds = list(db_session.scalars(select(KaspiXmlFeed)).all())
            jobs = list(db_session.scalars(select(BrowserAgentJob)).all())

            assert [item.name for item in products] == [expected_name]
            assert [item.code for item in suppliers] == ["ozon"]
            assert [item.external_order_id for item in orders] == [expected_order]
            assert [item.quantity_remaining for item in inventory] == [expected_quantity]
            assert len(policies) == len(feeds) == len(jobs) == 1
            assert db_session.scalar(select(func.count()).select_from(Product)) == 1

    db_session.expunge_all()
    with workspace_context(1):
        assert db_session.get(Product, second["product_id"]) is None
        db_session.execute(update(KaspiXmlFeed).values(active=False))
        db_session.commit()

    db_session.info["include_all_workspaces"] = True
    try:
        feed_states = dict(
            db_session.execute(select(KaspiXmlFeed.id, KaspiXmlFeed.active)).all()
        )
    finally:
        db_session.info.pop("include_all_workspaces", None)
    assert feed_states[first["feed_id"]] is False
    assert feed_states[second["feed_id"]] is True


def test_dumping_inventory_cards_are_isolated_between_accounts(db_session) -> None:
    _seed_workspace(db_session, 1, "barwork")
    _seed_workspace(db_session, 2, "leoxpress")
    db_session.commit()
    db_session.expunge_all()

    with workspace_context(1):
        barwork = list_dumping_products(db_session)
    db_session.expunge_all()
    with workspace_context(2):
        leoxpress = list_dumping_products(db_session)

    assert [(row["name"], row["inventory_on_hand"]) for row in barwork] == [
        ("Product 1", 1)
    ]
    assert [(row["name"], row["inventory_on_hand"]) for row in leoxpress] == [
        ("Product 2", 2)
    ]


def test_cross_workspace_write_is_rejected(db_session) -> None:
    db_session.add_all(
        (
            Workspace(id=1, name="BARWORK", slug="barwork", is_active=True),
            Workspace(id=2, name="SECOND", slug="second", is_active=True),
        )
    )
    db_session.commit()

    with workspace_context(1):
        db_session.add(
            Product(
                workspace_id=2,
                kaspi_product_id="foreign",
                name="Foreign product",
                status="active",
            )
        )
        with pytest.raises(WorkspaceIsolationError):
            db_session.flush()
        db_session.rollback()


def test_public_xml_has_one_stable_url_per_workspace(db_session) -> None:
    _seed_workspace(db_session, 1, "barwork")
    _seed_workspace(db_session, 2, "second-shop")
    db_session.commit()

    legacy = read_public_kaspi_feed(db_session)
    second = read_workspace_kaspi_feed("second-shop", db_session)

    assert legacy.body == b"<catalog>1</catalog>"
    assert second.body == b"<catalog>2</catalog>"


def test_unscoped_competitor_completion_updates_only_job_workspace_xml(
    db_session,
) -> None:
    first = _seed_workspace(db_session, 1, "barwork")
    second = _seed_workspace(db_session, 2, "leoxpress")
    db_session.commit()

    barwork_xml = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers><offer sku='same-sku'>
      <cityprices><cityprice cityId='750000000'>8000</cityprice></cityprices>
      <availability available='no' preOrder='0' stockCount='0'/>
    </offer></offers></kaspi_catalog>"""
    leoxpress_xml = """<?xml version='1.0' encoding='utf-8'?>
    <kaspi_catalog><offers><offer sku='same-sku'>
      <cityprices><cityprice cityId='750000000'>9000</cityprice></cityprices>
      <availability available='yes' preOrder='0' stockCount='2'/>
    </offer></offers></kaspi_catalog>"""

    db_session.info["include_all_workspaces"] = True
    try:
        barwork_feed = db_session.get(KaspiXmlFeed, first["feed_id"])
        leoxpress_feed = db_session.get(KaspiXmlFeed, second["feed_id"])
        barwork_feed.source_xml = barwork_feed.generated_xml = barwork_xml
        leoxpress_feed.source_xml = leoxpress_feed.generated_xml = leoxpress_xml

        policy = db_session.scalar(
            select(DumpingPolicy).where(
                DumpingPolicy.product_id == first["product_id"]
            )
        )
        db_session.add(
            DumpingRun(
                workspace_id=1,
                product_id=first["product_id"],
                dumping_policy_id=policy.id,
                status="ready",
                own_price_kzt=Decimal("8000"),
                published=True,
                explanation_json={},
            )
        )
        db_session.commit()

        with workspace_context(1):
            result = apply_competitor_snapshot(
                db_session,
                product_id=first["product_id"],
                market=KaspiCompetitorSnapshot(
                    own_price_kzt=None,
                    competitor_price_kzt=Decimal("7500"),
                    competitor_name="Competitor",
                    own_position=None,
                    seller_count=1,
                    product_url="https://kaspi.kz/shop/p/test/",
                ),
            )
        db_session.commit()

        assert result["decision"]["status"] != "suspended_seller_removed"
        assert policy.enabled is True
        assert 'cityId="750000000">7499</' in barwork_feed.generated_xml
        assert 'stockCount="1"' in barwork_feed.generated_xml
        assert leoxpress_feed.generated_xml == leoxpress_xml
    finally:
        db_session.info.pop("include_all_workspaces", None)


def test_existing_pages_share_one_account_context_script() -> None:
    static = ROOT / "backend" / "app" / "static"
    pages = (
        "dashboard.html",
        "products.html",
        "product-detail.html",
        "orders.html",
        "revenue.html",
        "dumping.html",
        "suppliers.html",
        "monitoring.html",
    )
    for name in pages:
        source = (static / name).read_text(encoding="utf-8")
        assert source.count("/static/workspace-context.js") == 1

    shared = (static / "workspace-context.js").read_text(encoding="utf-8")
    assert 'headers.set(workspaceHeader, String(selectedWorkspaceId()))' in shared
    assert 'localStorage.setItem(workspaceStorageKey, String(account.id))' in shared
    assert 'window.location.reload()' in shared
    assert "Подключить ещё один аккаунт" in shared


def test_workspace_header_scopes_existing_product_endpoint(db_session, monkeypatch) -> None:
    _seed_workspace(db_session, 1, "barwork")
    _seed_workspace(db_session, 2, "second-shop")
    db_session.commit()
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-service-token")

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        first = client.get(
            "/api/products",
            headers={"Authorization": "Bearer test-service-token"},
        )
        second = client.get(
            "/api/products",
            headers={
                "Authorization": "Bearer test-service-token",
                "X-Workspace-ID": "2",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert first.status_code == second.status_code == 200
    assert [item["name"] for item in first.json()] == ["Product 1"]
    assert [item["name"] for item in second.json()] == ["Product 2"]


def test_second_kaspi_account_is_created_without_exposing_token(db_session, monkeypatch) -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-service-token")
    monkeypatch.setenv("KASPI_CREDENTIALS_KEY", encryption_key)

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/workspaces?validate=false",
            headers={"Authorization": "Bearer test-service-token"},
            json={
                "name": "Second Kaspi",
                "partner_id": "partner-second",
                "api_token": "secret-second-token",
                "timezone": "Asia/Almaty",
            },
        )
        duplicate = client.post(
            "/api/workspaces?validate=false",
            headers={"Authorization": "Bearer test-service-token"},
            json={
                "name": "Duplicate",
                "partner_id": "partner-second",
                "api_token": "another-token",
                "timezone": "Asia/Almaty",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 201
    assert duplicate.status_code == 409
    body = response.json()
    assert body["configured"] is True
    assert "secret-second-token" not in response.text
    db_session.info["include_all_workspaces"] = True
    try:
        credential = db_session.scalar(
            select(KaspiAccountCredential).where(
                KaspiAccountCredential.workspace_id == body["id"]
            )
        )
    finally:
        db_session.info.pop("include_all_workspaces", None)
    assert credential is not None
    assert credential.api_token_encrypted != "secret-second-token"
    assert decrypt_api_token(credential.api_token_encrypted) == "secret-second-token"


def test_kaspi_connection_validation_starts_from_first_valid_page(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, settings):
            observed["api_token"] = settings.api_token

        def fetch_orders(self, *, cursor, updated_after, limit):
            observed.update(
                cursor=cursor,
                updated_after=updated_after,
                limit=limit,
            )

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(
        "backend.app.workspace_kaspi.KaspiHttpTransport",
        FakeTransport,
    )

    validate_kaspi_connection("secret-token")

    assert observed == {
        "api_token": "secret-token",
        "cursor": "1",
        "updated_after": None,
        "limit": 1,
        "closed": True,
    }


def test_multiaccount_migration_covers_runtime_tables() -> None:
    source = (
        ROOT
        / "migrations"
        / "versions"
        / "20260731_0028_multiaccount_runtime_isolation.py"
    ).read_text(encoding="utf-8")
    for table_name in OPERATIONAL_TABLES:
        assert f'"{table_name}"' in source
    assert 'down_revision: str | None = "20260731_0027"' in source
    assert '"uq_suppliers_workspace_code"' in source
    assert 'dialect.name == "postgresql"' in source
    assert "autocommit_block" in source
    assert "CREATE INDEX CONCURRENTLY" in source
    assert "NOT VALID" in source
