"""reconcile duplicate online supplier bindings

Revision ID: 20260731_0026
Revises: 20260729_0025
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import sqlalchemy as sa
from alembic import op


revision: str = "20260731_0026"
down_revision: str | None = "20260729_0025"
branch_labels: str | None = None
depends_on: str | None = None


def _canonical_identity(
    supplier_code: str,
    external_id: str,
    url: str,
) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    code = supplier_code.strip().casefold()

    if code == "ozon" and (
        host in {"ozon.ru", "ozon.kz"}
        or host.endswith(".ozon.ru")
        or host.endswith(".ozon.kz")
    ):
        match = re.search(
            r"(?:product|context/detail/id)/(?:[^/]*-)?(\d+)(?:/|$)",
            path,
        )
        return (match.group(1) if match else path.split("/")[-1]).casefold()

    if code == "wb" and (
        host in {"wildberries.ru", "wb.ru"}
        or host.endswith(".wildberries.ru")
        or host.endswith(".wb.ru")
    ):
        match = re.search(r"/catalog/(\d+)(?:/|$)", path)
        return (match.group(1) if match else path.split("/")[-1]).casefold()

    return external_id.strip().casefold()


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _close_xml_if_product_has_no_source(connection, product_id: int) -> None:
    product = connection.execute(
        sa.text(
            """
            SELECT
                p.workspace_id,
                p.kaspi_product_id,
                p.merchant_sku
            FROM products p
            JOIN dumping_policies dp ON dp.product_id = p.id
            WHERE p.id = :product_id
              AND dp.enabled = true
              AND dp.auto_publish_xml = true
            """
        ),
        {"product_id": product_id},
    ).mappings().one_or_none()
    if product is None:
        return

    inventory_count = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM inventory_batches
            WHERE product_id = :product_id
              AND quantity_remaining > 0
            """
        ),
        {"product_id": product_id},
    ).scalar_one()
    if int(inventory_count or 0) > 0:
        return

    source_count = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM product_bindings pb
            JOIN supplier_products sp ON sp.id = pb.supplier_product_id
            LEFT JOIN supplier_offer_states sos
                ON sos.supplier_product_id = sp.id
            WHERE pb.product_id = :product_id
              AND pb.status IN ('active', 'confirmed', 'degraded')
              AND (
                    (
                        sos.id IS NOT NULL
                        AND sos.price IS NOT NULL
                        AND (sos.available IS NULL OR sos.available = true)
                    )
                    OR
                    (
                        sos.id IS NULL
                        AND sp.current_price IS NOT NULL
                        AND (sp.in_stock IS NULL OR sp.in_stock = true)
                    )
              )
            """
        ),
        {"product_id": product_id},
    ).scalar_one()
    if int(source_count or 0) > 0:
        return

    feed = connection.execute(
        sa.text(
            """
            SELECT id, generated_xml, source_xml
            FROM kaspi_xml_feeds
            WHERE workspace_id = :workspace_id
              AND active = true
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"workspace_id": int(product["workspace_id"])},
    ).mappings().one_or_none()
    if feed is None:
        return

    xml_text = str(feed["generated_xml"] or feed["source_xml"])
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    identities = {
        str(product["kaspi_product_id"] or "").strip(),
        str(product["merchant_sku"] or "").strip(),
    }
    identities.discard("")
    offer = next(
        (
            element
            for element in root.iter()
            if _local_name(element.tag) == "offer"
            and (
                element.attrib.get("sku")
                or element.attrib.get("id")
                or ""
            ).strip()
            in identities
        ),
        None,
    )
    if offer is None:
        return

    availability = next(
        (
            element
            for element in offer.iter()
            if _local_name(element.tag) == "availability"
        ),
        None,
    )
    if availability is None:
        availability = ElementTree.SubElement(offer, "availability")
    availability.set("available", "no")
    availability.set("preOrder", "0")
    generated_xml = ElementTree.tostring(
        root,
        encoding="unicode",
        xml_declaration=True,
    )
    connection.execute(
        sa.text(
            """
            UPDATE kaspi_xml_feeds
            SET generated_xml = :generated_xml,
                generated_at = CURRENT_TIMESTAMP
            WHERE id = :feed_id
            """
        ),
        {
            "feed_id": int(feed["id"]),
            "generated_xml": generated_xml,
        },
    )


def _repair_duplicate_bindings(connection) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT
                pb.id AS binding_id,
                pb.product_id AS product_id,
                pb.status AS binding_status,
                pb.is_primary AS is_primary,
                pb.priority AS priority,
                sp.id AS supplier_product_id,
                sp.supplier_id AS supplier_id,
                sp.external_id AS external_id,
                sp.url AS supplier_url,
                sp.last_checked_at AS product_checked_at,
                sp.created_at AS product_created_at,
                s.code AS supplier_code,
                sos.last_checked_at AS state_checked_at,
                mt.id AS monitor_target_id
            FROM product_bindings pb
            JOIN supplier_products sp ON sp.id = pb.supplier_product_id
            JOIN suppliers s ON s.id = sp.supplier_id
            LEFT JOIN supplier_offer_states sos
                ON sos.supplier_product_id = sp.id
            LEFT JOIN monitor_targets mt
                ON mt.product_binding_id = pb.id
            WHERE pb.status IN ('active', 'confirmed', 'degraded')
              AND s.code IN ('ozon', 'wb')
            """
        )
    ).mappings().all()

    groups: dict[tuple[int, int, str], list[dict]] = defaultdict(list)
    for row in rows:
        identity = _canonical_identity(
            str(row["supplier_code"]),
            str(row["external_id"]),
            str(row["supplier_url"]),
        )
        groups[
            (
                int(row["product_id"]),
                int(row["supplier_id"]),
                identity,
            )
        ].append(dict(row))

    affected_product_ids: set[int] = set()
    for duplicates in groups.values():
        if len(duplicates) < 2:
            continue

        def winner_key(row: dict) -> tuple[datetime, bool, int, int]:
            checked_at = (
                row["state_checked_at"]
                or row["product_checked_at"]
                or row["product_created_at"]
            )
            return (
                _as_utc(checked_at),
                bool(row["is_primary"]),
                -int(row["priority"]),
                int(row["binding_id"]),
            )

        winner = max(duplicates, key=winner_key)
        losers = [
            row
            for row in duplicates
            if int(row["binding_id"]) != int(winner["binding_id"])
        ]
        preserve_primary = any(bool(row["is_primary"]) for row in duplicates)
        preserve_priority = min(int(row["priority"]) for row in duplicates)
        affected_product_ids.add(int(winner["product_id"]))

        connection.execute(
            sa.text(
                """
                UPDATE product_bindings
                SET status = 'active',
                    is_primary = :is_primary,
                    priority = :priority
                WHERE id = :binding_id
                """
            ),
            {
                "binding_id": int(winner["binding_id"]),
                "is_primary": preserve_primary,
                "priority": preserve_priority,
            },
        )

        for loser in losers:
            binding_id = int(loser["binding_id"])
            connection.execute(
                sa.text(
                    """
                    UPDATE product_bindings
                    SET status = 'disabled',
                        is_primary = false
                    WHERE id = :binding_id
                    """
                ),
                {"binding_id": binding_id},
            )
            target_id = loser["monitor_target_id"]
            if target_id is None:
                continue
            target_id = int(target_id)
            connection.execute(
                sa.text(
                    """
                    UPDATE monitor_targets
                    SET status = 'disabled'
                    WHERE id = :target_id
                    """
                ),
                {"target_id": target_id},
            )
            connection.execute(
                sa.text(
                    """
                    UPDATE browser_agent_jobs
                    SET status = 'failed',
                        error_code = 'duplicate_supplier_binding',
                        error_message =
                            'Задание отменено: источник объединён с канонической привязкой',
                        finished_at = CURRENT_TIMESTAMP
                    WHERE monitor_target_id = :target_id
                      AND status = 'queued'
                    """
                ),
                {"target_id": target_id},
            )

    for product_id in sorted(affected_product_ids):
        _close_xml_if_product_has_no_source(connection, product_id)


def upgrade() -> None:
    _repair_duplicate_bindings(op.get_bind())


def downgrade() -> None:
    # The records and their history are preserved. Automatically reactivating
    # superseded duplicates would restore the unsafe stale-price behaviour.
    pass
