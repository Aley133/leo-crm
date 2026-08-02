"""isolate operational data by workspace

Revision ID: 20260731_0028
Revises: 20260731_0027
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0028"
down_revision: str | None = "20260731_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_WORKSPACE_ID = 1

WORKSPACE_TABLES: tuple[str, ...] = (
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
)


def _single_column_supplier_code_uniques() -> tuple[list[str], list[str]]:
    inspector = sa.inspect(op.get_bind())
    constraint_names: list[str] = []
    index_names: list[str] = []
    for constraint in inspector.get_unique_constraints("suppliers"):
        columns = tuple(constraint.get("column_names") or ())
        name = constraint.get("name")
        if name and columns == ("code",):
            constraint_names.append(name)
    for index in inspector.get_indexes("suppliers"):
        columns = tuple(index.get("column_names") or ())
        name = index.get("name")
        if (
            name
            and index.get("unique") is True
            and columns == ("code",)
            and not index.get("duplicates_constraint")
        ):
            index_names.append(name)
    return constraint_names, index_names


def _quoted(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _autocommit_execute(statement: str) -> None:
    """Run one idempotent PostgreSQL DDL step without retaining prior locks."""

    with op.get_context().autocommit_block():
        op.execute(sa.text(statement))


def _postgresql_workspace_table_upgrade(table_name: str) -> None:
    """Upgrade one busy table using short, restart-safe lock windows."""

    inspector = sa.inspect(op.get_bind())
    table = _quoted(table_name)
    workspace_column = _quoted("workspace_id")
    foreign_key_name = f"fk_{table_name}_workspace_id_workspaces"
    index_name = f"ix_{table_name}_workspace_id"

    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "workspace_id" not in columns:
        _autocommit_execute(
            f"ALTER TABLE {table} ADD COLUMN {workspace_column} "
            f"INTEGER DEFAULT {LEGACY_WORKSPACE_ID} NOT NULL"
        )

    inspector = sa.inspect(op.get_bind())
    foreign_keys = {
        foreign_key.get("name")
        for foreign_key in inspector.get_foreign_keys(table_name)
        if foreign_key.get("name")
    }
    if foreign_key_name not in foreign_keys:
        _autocommit_execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {_quoted(foreign_key_name)} "
            f"FOREIGN KEY ({workspace_column}) REFERENCES {_quoted('workspaces')} "
            f"({_quoted('id')}) ON DELETE RESTRICT NOT VALID"
        )

    inspector = sa.inspect(op.get_bind())
    indexes = {
        index.get("name")
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }
    if index_name not in indexes:
        _autocommit_execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_quoted(index_name)} "
            f"ON {table} ({workspace_column})"
        )

    # VALIDATE uses a lighter lock than adding an immediately validated FK.
    # It is safe to repeat when a previous deploy completed only part of 0028.
    _autocommit_execute(
        f"ALTER TABLE {table} VALIDATE CONSTRAINT {_quoted(foreign_key_name)}"
    )


def _postgresql_unique_constraint(
    table_name: str,
    constraint_name: str,
    columns: tuple[str, ...],
) -> None:
    inspector = sa.inspect(op.get_bind())
    constraints = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    }
    if constraint_name in constraints:
        return

    index_name = f"{constraint_name}_idx"
    table = _quoted(table_name)
    quoted_columns = ", ".join(_quoted(column) for column in columns)
    _autocommit_execute(
        f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_quoted(index_name)} "
        f"ON {table} ({quoted_columns})"
    )
    _autocommit_execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {_quoted(constraint_name)} "
        f"UNIQUE USING INDEX {_quoted(index_name)}"
    )


def _postgresql_upgrade(existing_tables: set[str]) -> None:
    for table_name in WORKSPACE_TABLES:
        if table_name in existing_tables:
            _postgresql_workspace_table_upgrade(table_name)

    _postgresql_unique_constraint(
        "suppliers",
        "uq_suppliers_workspace_code",
        ("workspace_id", "code"),
    )

    old_constraint_names, old_index_names = _single_column_supplier_code_uniques()
    for constraint_name in old_constraint_names:
        _autocommit_execute(
            f"ALTER TABLE {_quoted('suppliers')} "
            f"DROP CONSTRAINT {_quoted(constraint_name)}"
        )
    for index_name in old_index_names:
        _autocommit_execute(f"DROP INDEX IF EXISTS {_quoted(index_name)}")

    _postgresql_unique_constraint(
        "kaspi_account_credentials",
        "uq_kaspi_account_credentials_partner",
        ("partner_id",),
    )


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if op.get_bind().dialect.name == "postgresql":
        _postgresql_upgrade(existing_tables)
        return

    for table_name in WORKSPACE_TABLES:
        if table_name not in existing_tables:
            continue
        columns = {
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns(table_name)
        }
        if "workspace_id" in columns:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "workspace_id",
                    sa.Integer(),
                    nullable=False,
                    server_default=str(LEGACY_WORKSPACE_ID),
                )
            )
            batch_op.create_foreign_key(
                f"fk_{table_name}_workspace_id_workspaces",
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_index(
                f"ix_{table_name}_workspace_id",
                ["workspace_id"],
            )

    old_constraint_names, old_index_names = _single_column_supplier_code_uniques()
    with op.batch_alter_table("suppliers") as batch_op:
        for constraint_name in old_constraint_names:
            batch_op.drop_constraint(constraint_name, type_="unique")
        for index_name in old_index_names:
            batch_op.drop_index(index_name)
        batch_op.create_unique_constraint(
            "uq_suppliers_workspace_code",
            ["workspace_id", "code"],
        )

    with op.batch_alter_table("kaspi_account_credentials") as batch_op:
        batch_op.create_unique_constraint(
            "uq_kaspi_account_credentials_partner",
            ["partner_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("kaspi_account_credentials") as batch_op:
        batch_op.drop_constraint(
            "uq_kaspi_account_credentials_partner",
            type_="unique",
        )

    inspector = sa.inspect(op.get_bind())
    supplier_uniques = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("suppliers")
        if tuple(constraint.get("column_names") or ()) == ("workspace_id", "code")
        and constraint.get("name")
    }
    with op.batch_alter_table("suppliers") as batch_op:
        for constraint_name in supplier_uniques:
            batch_op.drop_constraint(constraint_name, type_="unique")
        batch_op.create_unique_constraint("uq_suppliers_code", ["code"])

    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in reversed(WORKSPACE_TABLES):
        if table_name not in existing_tables:
            continue
        columns = {
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns(table_name)
        }
        if "workspace_id" not in columns:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_workspace_id")
            batch_op.drop_constraint(
                f"fk_{table_name}_workspace_id_workspaces",
                type_="foreignkey",
            )
            batch_op.drop_column("workspace_id")
