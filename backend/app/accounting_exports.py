from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
import re
from typing import Any, Iterable
from xml.etree import ElementTree
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def build_inventory_xml(snapshot: dict[str, Any], *, generated_at: datetime) -> bytes:
    root = ElementTree.Element(
        "inventoryExport",
        {
            "workspaceId": str(snapshot["workspace_id"]),
            "currency": str(snapshot.get("currency") or "KZT"),
            "generatedAt": _text(generated_at),
            "mode": "read-only",
        },
    )
    ElementTree.SubElement(
        root,
        "summary",
        {
            "catalogPositions": str(snapshot["catalog_positions"]),
            "skuCount": str(snapshot["sku_count"]),
            "onHandUnits": str(snapshot["on_hand_units"]),
            "pricedOnHandUnits": str(snapshot["priced_on_hand_units"]),
            "unpricedUnits": str(snapshot["unpriced_units"]),
            "inventoryValue": _text(snapshot["inventory_value"]),
            "incomingUnits": str(snapshot["incoming_units"]),
            "incomingValue": _text(snapshot["incoming_value"]),
        },
    )
    products = ElementTree.SubElement(root, "products")
    for item in snapshot["items"]:
        product = ElementTree.SubElement(
            products,
            "product",
            {
                "id": str(item["product_id"]),
                "kaspiProductId": _text(item.get("kaspi_product_id")),
                "merchantSku": _text(item.get("merchant_sku")),
            },
        )
        fields = (
            ("name", item.get("name")),
            ("brand", item.get("brand")),
            ("onHandUnits", item.get("on_hand_units")),
            ("pricedOnHandUnits", item.get("priced_on_hand_units")),
            ("unpricedUnits", item.get("unpriced_units")),
            ("weightedPurchasePrice", item.get("weighted_purchase_price")),
            ("lastPurchasePrice", item.get("last_purchase_price")),
            ("inventoryValue", item.get("inventory_value")),
            ("incomingUnits", item.get("incoming_units")),
            ("incomingUnpricedUnits", item.get("incoming_unpriced_units")),
            ("incomingValue", item.get("incoming_value")),
            ("currentBatchesCount", item.get("current_batches_count")),
            ("oldestBatchAt", item.get("oldest_batch_at")),
            ("lastPurchaseAt", item.get("last_purchase_at")),
            ("sharedCardsCount", item.get("shared_cards_count")),
        )
        for tag, value in fields:
            ElementTree.SubElement(product, tag).text = _text(value)
        shared_skus = ElementTree.SubElement(product, "sharedMerchantSkus")
        for sku in item.get("shared_merchant_skus", []):
            ElementTree.SubElement(shared_skus, "sku").text = _text(sku)
        shared_kaspi_ids = ElementTree.SubElement(product, "sharedKaspiProductIds")
        for kaspi_id in item.get("shared_kaspi_product_ids", []):
            ElementTree.SubElement(shared_kaspi_ids, "kaspiProductId").text = _text(kaspi_id)
        batches = ElementTree.SubElement(product, "batches")
        for batch in [*item.get("batches", []), *item.get("incoming_batches", [])]:
            batch_element = ElementTree.SubElement(
                batches,
                "batch",
                {
                    "id": str(batch["batch_id"]),
                    "state": str(batch["state"]),
                },
            )
            for tag, value in (
                ("receivedAt", batch.get("received_at")),
                ("quantityReceived", batch.get("quantity_received")),
                ("quantityRemaining", batch.get("quantity_remaining")),
                ("unitCost", batch.get("unit_cost")),
                ("remainingValue", batch.get("remaining_value")),
                ("sourceName", batch.get("source_name")),
                ("reference", batch.get("reference")),
                ("note", batch.get("note")),
            ):
                ElementTree.SubElement(batch_element, tag).text = _text(value)

    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _column_name(number: int) -> str:
    value = number
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _safe_xml_text(value: Any) -> str:
    return escape(_ILLEGAL_XML.sub("", _text(value)))


def _cell(reference: str, value: Any, *, style: int = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{reference}"{style_attr}><v>{_text(value)}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"{style_attr}>'
        f'<is><t xml:space="preserve">{_safe_xml_text(value)}</t></is></c>'
    )


def _sheet_xml(
    rows: Iterable[list[Any]],
    *,
    widths: list[float],
    money_columns: set[int] | None = None,
    integer_columns: set[int] | None = None,
    autofilter: bool = False,
) -> str:
    materialized = list(rows)
    max_columns = max((len(row) for row in materialized), default=1)
    max_rows = max(len(materialized), 1)
    money_columns = money_columns or set()
    integer_columns = integer_columns or set()
    row_xml: list[str] = []
    for row_index, row in enumerate(materialized, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            style = 1 if row_index == 1 else 2 if column_index in money_columns else 3 if column_index in integer_columns else 0
            cells.append(
                _cell(f"{_column_name(column_index)}{row_index}", value, style=style)
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    filter_xml = (
        f'<autoFilter ref="A1:{_column_name(max_columns)}{max_rows}"/>'
        if autofilter and materialized
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{_column_name(max_columns)}{max_rows}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<cols>{columns}</cols><sheetData>{"".join(row_xml)}</sheetData>{filter_xml}'
        '</worksheet>'
    )


def build_inventory_xlsx(snapshot: dict[str, Any], *, generated_at: datetime) -> bytes:
    summary_rows = [
        ["Показатель", "Значение"],
        ["Рабочее пространство", snapshot["workspace_id"]],
        ["Сформировано", generated_at.isoformat()],
        ["Валюта", snapshot.get("currency") or "KZT"],
        ["Позиций каталога", snapshot["catalog_positions"]],
        ["SKU с физическим остатком", snapshot["sku_count"]],
        ["Физический остаток, ед.", snapshot["on_hand_units"]],
        ["Единиц с закупочной ценой", snapshot["priced_on_hand_units"]],
        ["Единиц без закупочной цены", snapshot["unpriced_units"]],
        ["Стоимость физического остатка, KZT", snapshot["inventory_value"]],
        ["Товар в пути, ед.", snapshot["incoming_units"]],
        ["Стоимость товара в пути, KZT", snapshot["incoming_value"]],
        ["Важно", "Файл сформирован только для чтения; партии и FIFO не изменены."],
    ]
    inventory_rows: list[list[Any]] = [[
        "Product ID",
        "Kaspi ID",
        "Артикул",
        "Название",
        "Бренд",
        "Физический остаток",
        "Оценённых единиц",
        "Без закупочной цены",
        "Средняя закупочная цена",
        "Последняя закупочная цена",
        "Стоимость остатка",
        "В пути",
        "Стоимость в пути",
        "Текущих партий",
        "Старейшая партия",
        "Последняя закупка",
        "Связанные артикулы",
        "Связанные Kaspi ID",
    ]]
    for item in snapshot["items"]:
        inventory_rows.append([
            item["product_id"],
            item.get("kaspi_product_id"),
            item.get("merchant_sku"),
            item.get("name"),
            item.get("brand"),
            item.get("on_hand_units"),
            item.get("priced_on_hand_units"),
            item.get("unpriced_units"),
            item.get("weighted_purchase_price"),
            item.get("last_purchase_price"),
            item.get("inventory_value"),
            item.get("incoming_units"),
            item.get("incoming_value"),
            item.get("current_batches_count"),
            item.get("oldest_batch_at"),
            item.get("last_purchase_at"),
            ", ".join(item.get("shared_merchant_skus", [])),
            ", ".join(item.get("shared_kaspi_product_ids", [])),
        ])
    batch_rows: list[list[Any]] = [[
        "Партия ID",
        "Состояние",
        "Product ID",
        "Kaspi ID",
        "Артикул",
        "Название",
        "Дата партии",
        "Принято / ожидается",
        "Физический остаток",
        "Цена закупки",
        "Стоимость остатка / поставки",
        "Источник",
        "Документ / ссылка",
        "Примечание",
    ]]
    for item in snapshot["items"]:
        for batch in [*item.get("batches", []), *item.get("incoming_batches", [])]:
            batch_rows.append([
                batch.get("batch_id"),
                "На складе" if batch.get("state") == "on_hand" else "В пути",
                item.get("product_id"),
                item.get("kaspi_product_id"),
                item.get("merchant_sku"),
                item.get("name"),
                batch.get("received_at"),
                batch.get("quantity_received"),
                batch.get("quantity_remaining"),
                batch.get("unit_cost"),
                batch.get("remaining_value"),
                batch.get("source_name"),
                batch.get("reference"),
                batch.get("note"),
            ])

    summary_sheet = _sheet_xml(
        summary_rows,
        widths=[38, 72],
        money_columns={2},
        integer_columns={2},
    )
    inventory_sheet = _sheet_xml(
        inventory_rows,
        widths=[12, 18, 20, 56, 22, 18, 18, 20, 22, 24, 22, 12, 20, 16, 23, 23, 42, 42],
        money_columns={9, 10, 11, 13},
        integer_columns={1, 6, 7, 8, 12, 14},
        autofilter=True,
    )
    batches_sheet = _sheet_xml(
        batch_rows,
        widths=[13, 14, 12, 18, 20, 52, 23, 20, 20, 18, 27, 26, 26, 48],
        money_columns={10, 11},
        integer_columns={1, 3, 8, 9},
        autofilter=True,
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Сводка" sheetId="1" r:id="rId1"/>'
        '<sheet name="Остатки" sheetId="2" r:id="rId2"/>'
        '<sheet name="Партии FIFO" sheetId="3" r:id="rId3"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="# ##0.00"/></numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    timestamp = generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>LEO CRM</dc:creator><dc:title>Остатки и закупочные цены</dc:title>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        '</cp:coreProperties>'
    )
    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>LEO CRM</Application></Properties>'
    )

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("docProps/app.xml", app)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", summary_sheet)
        archive.writestr("xl/worksheets/sheet2.xml", inventory_sheet)
        archive.writestr("xl/worksheets/sheet3.xml", batches_sheet)
    return output.getvalue()
