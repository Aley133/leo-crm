"""assign products to workspaces

Revision ID: 20260727_0019
Revises: 20260727_0018
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20260727_0019"
down_revision: str | None = "20260727_0018"
branch_labels: str | None = None
depends_on: str | None = None

LEGACY_WORKSPACE_ID = 1


def _single_column_unique_objects() -> tuple[list[str], list[str]]:
    """Return actual unique constraints/indexes for products.kaspi_product_id.

    Older databases were created through different migration paths, so the
    original uniqueness may be named ``products_kaspi_product_id_key`` or may
    exist only as a unique index. Never assume one fixed constraint name.
    """

    inspector = sa.inspect(op.get_bind())
    constraint_names: list[str] = []
    index_names: list[str] = []

    for constraint in inspector.get_unique_constraints("products"):
        columns = tuple(constraint.get("column_names") or ())
        name = constraint.get("name")
        if name and columns == ("kaspi_product_id",):
            constraint_names.append(name)

    for index in inspector.get_indexes("products"):
        columns = tuple(index.get("column_names") or ())
        name = index.get("name")
        duplicates_constraint = index.get("duplicates_constraint")
        if (
            name
            and index.get("unique") is True
            and columns == ("kaspi_product_id",)
            and not duplicates_constraint
        ):
            index_names.append(name)

    return constraint_names, index_names


def upgrade() -> None:
    old_constraint_names, old_index_names = _single_column_unique_objects()

    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "workspace_id",
                sa.Integer(),
                nullable=False,
                server_default=str(LEGACY_WORKSPACE_ID),
            )
        )
        batch_op.create_foreign_key(
            "fk_products_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index("ix_products_workspace_id", ["workspace_id"])

        for constraint_name in old_constraint_names:
            batch_op.drop_constraint(constraint_name, type_="unique")
        for index_name in old_index_names:
            batch_op.drop_index(index_name)

        batch_op.create_unique_constraint(
            "uq_products_workspace_kaspi_product_id",
            ["workspace_id", "kaspi_product_id"],
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("workspace_id", server_default=None)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    unique_names = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("products")
        if tuple(constraint.get("column_names") or ())
        == ("workspace_id", "kaspi_product_id")
        and constraint.get("name")
    }

    with op.batch_alter_table("products") as batch_op:
        for constraint_name in unique_names:
            batch_op.drop_constraint(constraint_name, type_="unique")
        batch_op.create_unique_constraint(
            "uq_products_kaspi_product_id",
            ["kaspi_product_id"],
        )
        batch_op.drop_index("ix_products_workspace_id")
        batch_op.drop_constraint(
            "fk_products_workspace_id_workspaces",
            type_="foreignkey",
        )
        batch_op.drop_column("workspace_id")
