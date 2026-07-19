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
