from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.accounting_exports import build_inventory_xlsx, build_inventory_xml
from backend.app.accounting_models import AccountingCapitalSnapshot
from backend.app.accounting_service import (
    build_accounting_report,
    build_inventory_snapshot,
    record_capital_snapshot,
)
from backend.app.db import get_db
from backend.app.inventory_models import InventoryAllocation, InventoryBatch
from backend.app.main import app
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine, Product
from backend.app.workspace_context import workspace_context
from backend.app.workspace_models import Workspace


ROOT = Path(__file__).resolve().parents[1]


def _order(
    db_session,
    *,
    account: MarketplaceAccount,
    product: Product,
    code: str,
    status: str,
    amount: Decimal,
    ordered_at: datetime,
) -> tuple[MarketplaceOrder, MarketplaceOrderLine]:
    order = MarketplaceOrder(
        workspace_id=1,
        marketplace_account_id=account.id,
        external_order_id=f"external-{code}",
        external_code=code,
        status=status,
        original_status=status.upper(),
        currency="KZT",
        total_amount=amount,
        ordered_at=ordered_at,
        delivered_at=ordered_at if status == "delivered" else None,
    )
    db_session.add(order)
    db_session.flush()
    line = MarketplaceOrderLine(
        workspace_id=1,
        marketplace_order_id=order.id,
        external_line_id=f"line-{code}",
        product_id=product.id,
        external_product_id=product.kaspi_product_id,
        merchant_sku=product.merchant_sku,
        title=product.name,
        quantity=1,
        unit_price=amount,
        line_total=amount,
    )
    db_session.add(line)
    db_session.flush()
    return order, line


def _seed_accounting(db_session) -> tuple[datetime, Product, InventoryBatch]:
    now = datetime.now(UTC).replace(microsecond=0)
    account = MarketplaceAccount(
        workspace_id=1,
        provider="kaspi",
        external_account_id="barwork",
        display_name="BARWORK",
        timezone="Asia/Qyzylorda",
    )
    product = Product(
        workspace_id=1,
        kaspi_product_id="101",
        merchant_sku="SKU-101",
        name="Тестовый товар",
        brand="GLS",
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    batch = InventoryBatch(
        workspace_id=1,
        product_id=product.id,
        received_at=now - timedelta(days=60),
        quantity_received=10,
        quantity_remaining=7,
        unit_cost=Decimal("4000"),
        is_received=True,
        batch_type="purchase",
        source_name="Поставщик",
    )
    db_session.add(batch)
    db_session.flush()

    _delivered, delivered_line = _order(
        db_session,
        account=account,
        product=product,
        code="delivered-current",
        status="delivered",
        amount=Decimal("10000"),
        ordered_at=now - timedelta(days=1),
    )
    db_session.add(
        InventoryAllocation(
            workspace_id=1,
            inventory_batch_id=batch.id,
            marketplace_order_line_id=delivered_line.id,
            quantity=1,
            unit_cost=Decimal("4000"),
            allocated_at=now - timedelta(days=1),
        )
    )
    _order(
        db_session,
        account=account,
        product=product,
        code="cancelled-current",
        status="cancelled",
        amount=Decimal("5000"),
        ordered_at=now - timedelta(days=2),
    )
    _order(
        db_session,
        account=account,
        product=product,
        code="returned-current",
        status="returned",
        amount=Decimal("3000"),
        ordered_at=now - timedelta(days=3),
    )
    _order(
        db_session,
        account=account,
        product=product,
        code="delivered-previous",
        status="delivered",
        amount=Decimal("5000"),
        ordered_at=now - timedelta(days=40),
    )
    db_session.commit()
    return now, product, batch


def test_accounting_report_calculates_profit_losses_and_abc(db_session) -> None:
    now, product, _batch = _seed_accounting(db_session)

    report = build_accounting_report(
        db_session,
        workspace_id=1,
        days=30,
        as_of=now,
    )

    summary = report["summary"]
    assert summary["orders_total"] == 3
    assert summary["delivered_orders"] == 1
    assert summary["delivered_revenue"] == Decimal("10000.00")
    assert summary["cancelled_demand"] == Decimal("5000.00")
    assert summary["returned_value"] == Decimal("3000.00")
    assert summary["procurement_cost"] == Decimal("4000.00")
    assert summary["kaspi_commission"] == Decimal("1200.00")
    assert summary["tax"] == Decimal("300.00")
    assert summary["logistics"] == Decimal("1507.00")
    assert summary["net_profit"] == Decimal("2993.00")
    assert summary["result_is_complete"] is True
    assert summary["cost_coverage_pct"] == Decimal("100.00")

    assert report["inventory"]["inventory_value"] == Decimal("28000.00")
    assert report["capital"]["warehouse_at_cost"] == Decimal("28000.00")
    assert report["capital"]["cash_balance"] is None
    assert report["capital"]["total_capital"] is None
    assert report["comparison"]["delivered_revenue_change_pct"] == Decimal("100.00")
    product_row = next(row for row in report["products"] if row["product_id"] == product.id)
    assert product_row["abc_class"] == "A"
    assert product_row["known_net_profit"] == Decimal("2993.00")
    assert product_row["on_hand_units"] == 7
    assert product_row["weighted_purchase_price"] == Decimal("4000.00")


def test_capital_position_separates_cash_stock_transit_without_double_counting_free(
    db_session,
) -> None:
    now, product, _batch = _seed_accounting(db_session)
    db_session.add(Workspace(id=1, name="BARWORK", slug="barwork", is_active=True))
    db_session.add(
        InventoryBatch(
            workspace_id=1,
            product_id=product.id,
            received_at=now + timedelta(days=7),
            quantity_received=3,
            quantity_remaining=3,
            unit_cost=Decimal("2500"),
            is_received=False,
            batch_type="purchase",
            source_name="Белый ввоз",
        )
    )
    record_capital_snapshot(
        db_session,
        workspace_id=1,
        cash_balance_kzt=Decimal("2000000"),
        free_capital_kzt=Decimal("500000"),
        note="Резерв на закупки",
    )
    db_session.commit()

    report = build_accounting_report(db_session, workspace_id=1, days=30, as_of=now)
    capital = report["capital"]

    assert capital["workspace_name"] == "BARWORK"
    assert capital["cash_balance"] == Decimal("2000000.00")
    assert capital["warehouse_at_cost"] == Decimal("28000.00")
    assert capital["goods_in_transit"] == Decimal("7500.00")
    assert capital["free_capital"] == Decimal("500000.00")
    assert capital["total_capital"] == Decimal("2035500.00")
    assert capital["known_total_capital"] == Decimal("2035500.00")
    assert capital["free_capital_is_part_of_cash"] is True
    assert capital["formula"] == "cash_balance + warehouse_at_cost + goods_in_transit"


def test_capital_snapshots_are_append_only_and_workspace_isolated(db_session) -> None:
    db_session.add_all(
        [
            Workspace(id=1, name="BARWORK", slug="barwork", is_active=True),
            Workspace(id=2, name="LeoXpress", slug="leoxpress", is_active=True),
        ]
    )
    record_capital_snapshot(
        db_session,
        workspace_id=1,
        cash_balance_kzt=Decimal("100000"),
        free_capital_kzt=Decimal("25000"),
    )
    record_capital_snapshot(
        db_session,
        workspace_id=1,
        cash_balance_kzt=Decimal("120000"),
        free_capital_kzt=Decimal("30000"),
    )
    with workspace_context(2):
        record_capital_snapshot(
            db_session,
            workspace_id=2,
            cash_balance_kzt=Decimal("900000"),
            free_capital_kzt=Decimal("400000"),
        )
    db_session.commit()

    assert db_session.scalar(select(func.count(AccountingCapitalSnapshot.id))) == 2
    with workspace_context(2):
        assert db_session.scalar(select(func.count(AccountingCapitalSnapshot.id))) == 1
        report = build_accounting_report(db_session, workspace_id=2, days=30)
    assert report["capital"]["workspace_name"] == "LeoXpress"
    assert report["capital"]["cash_balance"] == Decimal("900000.00")


def test_inventory_exports_are_valid_xml_and_xlsx(db_session) -> None:
    now, product, _batch = _seed_accounting(db_session)
    snapshot = build_inventory_snapshot(db_session, workspace_id=1)
    snapshot.pop("owner_by_product")
    snapshot.pop("products_by_id")

    xml_document = build_inventory_xml(snapshot, generated_at=now)
    root = ElementTree.fromstring(xml_document)
    assert root.tag == "inventoryExport"
    assert root.attrib["workspaceId"] == "1"
    exported_product = root.find("./products/product")
    assert exported_product is not None
    assert exported_product.attrib["id"] == str(product.id)
    assert exported_product.findtext("onHandUnits") == "7"
    assert exported_product.findtext("weightedPurchasePrice") == "4000.00"
    exported_batch = exported_product.find("./batches/batch")
    assert exported_batch is not None
    assert exported_batch.attrib["state"] == "on_hand"
    assert exported_batch.findtext("unitCost") == "4000.00"

    xlsx_document = build_inventory_xlsx(snapshot, generated_at=now)
    with ZipFile(BytesIO(xlsx_document)) as archive:
        names = set(archive.namelist())
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/worksheets/sheet2.xml" in names
        assert "xl/worksheets/sheet3.xml" in names
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        inventory_sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
        batches_sheet = archive.read("xl/worksheets/sheet3.xml").decode("utf-8")
    assert "Сводка" in workbook
    assert "Остатки" in workbook
    assert "Партии FIFO" in workbook
    assert "Тестовый товар" in inventory_sheet
    assert "4000.00" in inventory_sheet
    assert "Тестовый товар" in batches_sheet
    assert "4000.00" in batches_sheet


def test_inventory_export_does_not_duplicate_shared_physical_stock(db_session) -> None:
    now, owner, _batch = _seed_accounting(db_session)
    shared_card = Product(
        workspace_id=1,
        kaspi_product_id="102",
        merchant_sku="SKU-102",
        inventory_owner_product_id=owner.id,
        name="Тестовый товар — вторая карточка",
        brand="GLS",
        status="active",
    )
    db_session.add(shared_card)
    db_session.commit()

    snapshot = build_inventory_snapshot(db_session, workspace_id=1)

    assert snapshot["on_hand_units"] == 7
    assert snapshot["inventory_value"] == Decimal("28000.00")
    owner_rows = [row for row in snapshot["items"] if row["product_id"] == owner.id]
    assert len(owner_rows) == 1
    assert owner_rows[0]["shared_cards_count"] == 2
    assert owner_rows[0]["shared_merchant_skus"] == ["SKU-101", "SKU-102"]
    assert owner_rows[0]["last_purchase_at"] == now - timedelta(days=60)


def test_accounting_report_keeps_workspaces_isolated(db_session) -> None:
    now, _product, _batch = _seed_accounting(db_session)
    db_session.info["include_all_workspaces"] = True
    account = MarketplaceAccount(
        workspace_id=2,
        provider="kaspi",
        external_account_id="leoxpress",
        display_name="LeoXpress",
        timezone="Asia/Qyzylorda",
    )
    product = Product(
        workspace_id=2,
        kaspi_product_id="201",
        merchant_sku="LEO-201",
        name="Товар другого аккаунта",
        status="active",
    )
    db_session.add_all([account, product])
    db_session.flush()
    order = MarketplaceOrder(
        workspace_id=2,
        marketplace_account_id=account.id,
        external_order_id="external-workspace-2",
        external_code="workspace-2",
        status="delivered",
        original_status="DELIVERED",
        currency="KZT",
        total_amount=Decimal("999999"),
        ordered_at=now - timedelta(days=1),
        delivered_at=now - timedelta(days=1),
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        MarketplaceOrderLine(
            workspace_id=2,
            marketplace_order_id=order.id,
            external_line_id="line-workspace-2",
            product_id=product.id,
            external_product_id=product.kaspi_product_id,
            merchant_sku=product.merchant_sku,
            title=product.name,
            quantity=1,
            unit_price=Decimal("999999"),
            line_total=Decimal("999999"),
        )
    )
    db_session.commit()
    db_session.info.pop("include_all_workspaces")

    report = build_accounting_report(db_session, workspace_id=1, days=30, as_of=now)
    with workspace_context(2):
        second_report = build_accounting_report(
            db_session,
            workspace_id=2,
            days=30,
            as_of=now,
        )

    assert report["summary"]["delivered_revenue"] == Decimal("10000.00")
    assert all(row["name"] != "Товар другого аккаунта" for row in report["products"])
    assert second_report["summary"]["delivered_revenue"] == Decimal("999999.00")
    assert [row["name"] for row in second_report["products"]] == [
        "Товар другого аккаунта"
    ]


def test_stock_without_period_sales_is_visible_as_frozen_capital(db_session) -> None:
    now, _sold_product, _batch = _seed_accounting(db_session)
    product = Product(
        workspace_id=1,
        kaspi_product_id="103",
        merchant_sku="SKU-103",
        name="Товар без продаж",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        InventoryBatch(
            workspace_id=1,
            product_id=product.id,
            received_at=now - timedelta(days=10),
            quantity_received=4,
            quantity_remaining=4,
            unit_cost=Decimal("1250"),
            is_received=True,
            batch_type="purchase",
        )
    )
    db_session.commit()

    report = build_accounting_report(db_session, workspace_id=1, days=30, as_of=now)
    row = next(item for item in report["products"] if item["product_id"] == product.id)

    assert row["name"] == "Товар без продаж"
    assert row["delivered_revenue"] == Decimal("0.00")
    assert row["on_hand_units"] == 4
    assert row["inventory_value"] == Decimal("5000.00")
    assert row["signal"] == "frozen_capital"


def test_accounting_http_report_and_downloads(db_session, monkeypatch) -> None:
    _seed_accounting(db_session)
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-service-token")

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer test-service-token"}
        report = client.get("/api/accounting/report?days=30", headers=headers)
        xml_export = client.get(
            "/api/accounting/inventory/export?format=xml",
            headers=headers,
        )
        xlsx_export = client.get(
            "/api/accounting/inventory/export?format=xlsx",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert report.status_code == 200
    assert report.json()["calculation"]["read_only"] is True
    assert report.json()["summary"]["delivered_revenue"] == "10000.00"
    assert xml_export.status_code == 200
    assert xml_export.headers["content-type"].startswith("application/xml")
    assert "workspace-1" in xml_export.headers["content-disposition"]
    assert ElementTree.fromstring(xml_export.content).tag == "inventoryExport"
    assert xlsx_export.status_code == 200
    assert xlsx_export.content.startswith(b"PK")
    assert xlsx_export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_accounting_capital_http_snapshot_is_append_only(db_session, monkeypatch) -> None:
    _seed_accounting(db_session)
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-service-token")

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer test-service-token"}
        first = client.post(
            "/api/accounting/capital-snapshot",
            headers=headers,
            json={
                "cash_balance_kzt": "2000000.00",
                "free_capital_kzt": "500000.00",
                "note": "Резерв",
            },
        )
        second = client.post(
            "/api/accounting/capital-snapshot",
            headers=headers,
            json={
                "cash_balance_kzt": "2100000.00",
                "free_capital_kzt": "600000.00",
            },
        )
        invalid = client.post(
            "/api/accounting/capital-snapshot",
            headers=headers,
            json={
                "cash_balance_kzt": "100000.00",
                "free_capital_kzt": "100001.00",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert first.status_code == 201
    assert first.json()["total_capital"] == "2028000.00"
    assert second.status_code == 201
    assert second.json()["cash_balance"] == "2100000.00"
    assert second.json()["free_capital"] == "600000.00"
    assert invalid.status_code == 422
    assert db_session.scalar(select(func.count(AccountingCapitalSnapshot.id))) == 2


def test_accounting_ui_and_api_preserve_business_data_contracts() -> None:
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    ui = (ROOT / "backend/app/ui.py").read_text(encoding="utf-8")
    api = (ROOT / "backend/app/accounting_api.py").read_text(encoding="utf-8")
    html = (ROOT / "backend/app/static/accounting.html").read_text(encoding="utf-8")
    script = (ROOT / "backend/app/static/accounting.js").read_text(encoding="utf-8")

    assert "app.include_router(accounting_router)" in main
    assert '@router.get("/crm/accounting"' in ui
    assert '@router.get("/report")' in api
    assert '@router.get("/inventory/export")' in api
    assert '@router.post("/capital-snapshot"' in api
    assert "@router.put" not in api
    assert "@router.delete" not in api
    assert "record_capital_snapshot(" in api
    assert 'id="summary-result"' in html
    assert 'id="capital-cash"' in html
    assert 'id="capital-warehouse"' in html
    assert 'id="capital-transit"' in html
    assert 'id="capital-free"' in html
    assert 'id="capital-total"' in html
    assert 'data-format="xml"' in html
    assert 'data-format="xlsx"' in html
    assert "/api/accounting/report" in script
    assert "/api/accounting/capital-snapshot" in script
    assert "/api/accounting/inventory/export" in script


def test_every_crm_navigation_exposes_accounting_tab() -> None:
    for path in (ROOT / "backend/app/static").glob("*.html"):
        source = path.read_text(encoding="utf-8")
        if '<nav aria-label="Основная навигация">' in source:
            assert 'href="/crm/accounting"' in source, path.name
