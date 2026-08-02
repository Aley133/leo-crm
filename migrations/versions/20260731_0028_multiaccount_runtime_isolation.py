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


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
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
