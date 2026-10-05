from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    return value


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    return config


def _reset_public_schema() -> None:
    engine = create_engine(_database_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def test_product_identity_migration_preserves_existing_data_and_imports_long_skus():
    from sqlalchemy.orm import Session
    from backend.app.product_xml_import_api import _commit_xml_import
    from backend.app.models import Product

    _reset_public_schema()
    config = _alembic_config()
    engine = create_engine(_database_url())
    try:
        command.upgrade(config, "20260928_0047")
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO products (kaspi_product_id, name, status) "
                "VALUES ('EXISTING', 'Existing product', 'active')"
            ))
        command.upgrade(config, "head")
        column = next(c for c in inspect(engine).get_columns("products") if c["name"] == "kaspi_product_id")
        assert column["type"].length == 128
        identities = ["K" * 64 + "A", "K" * 64 + "B" * 64]
        offers = "".join(f"<offer sku='{value}'><model>Long SKU</model></offer>" for value in identities)
        xml = f"<kaspi_catalog><offers>{offers}</offers></kaspi_catalog>".encode()
        with Session(engine, expire_on_commit=False) as session:
            result = _commit_xml_import(xml, source_filename="archive.xml", db=session)
            assert result["created_count"] == 2
            assert result["catalog_total"] == 3
            assert {p.kaspi_product_id for p in session.query(Product)} == {"EXISTING", *identities}
            assert _commit_xml_import(xml, source_filename="archive.xml", db=session)["created_count"] == 0
    finally:
        engine.dispose()
        _reset_public_schema()


def test_upgrade_0005_to_0006_backfills_existing_source_health_and_round_trips() -> None:
    _reset_public_schema()
    config = _alembic_config()

    try:
        command.upgrade(config, "20260719_0005")

        engine = create_engine(_database_url())
        try:
            with engine.begin() as connection:
                supplier_id = connection.scalar(
                    text(
                        "INSERT INTO suppliers (code, name) "
                        "VALUES ('migration-smoke', 'Migration Smoke') RETURNING id"
                    )
                )
                connection.execute(
                    text("INSERT INTO source_health (supplier_id) VALUES (:supplier_id)"),
                    {"supplier_id": supplier_id},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "20260719_0006")

        engine = create_engine(_database_url())
        try:
            inspector = inspect(engine)
            columns = {column["name"] for column in inspector.get_columns("source_health")}
            assert "access_strategy" in columns

            with engine.begin() as connection:
                strategy = connection.scalar(
                    text(
                        "SELECT access_strategy FROM source_health "
                        "WHERE supplier_id = :supplier_id"
                    ),
                    {"supplier_id": supplier_id},
                )
                assert strategy == "direct_http"

                connection.execute(
                    text(
                        "INSERT INTO source_health (supplier_id, access_strategy) "
                        "VALUES (:supplier_id, 'browser')"
                    ),
                    {"supplier_id": supplier_id},
                )

        finally:
            engine.dispose()

        # Remove the second strategy row so the documented downgrade precondition holds.
        engine = create_engine(_database_url())
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM source_health "
                        "WHERE supplier_id = :supplier_id AND access_strategy = 'browser'"
                    ),
                    {"supplier_id": supplier_id},
                )
        finally:
            engine.dispose()

        command.downgrade(config, "20260719_0005")

        engine = create_engine(_database_url())
        try:
            inspector = inspect(engine)
            columns = {column["name"] for column in inspector.get_columns("source_health")}
            assert "access_strategy" not in columns
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM source_health")) == 1
        finally:
            engine.dispose()

        command.upgrade(config, "head")

        engine = create_engine(_database_url())
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT access_strategy FROM source_health")
                ) == "direct_http"
        finally:
            engine.dispose()
    finally:
        _reset_public_schema()
