"""Add shared inventory ownership and audited order-stage overrides.

Revision ID: 20260803_0029
Revises: 20260731_0028
"""

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0029"
down_revision: str | None = "20260731_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("inventory_owner_product_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_products_inventory_owner_product_id",
        "products",
        "products",
        ["inventory_owner_product_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_products_inventory_owner_product_id",
        "products",
        ["inventory_owner_product_id"],
    )

    op.add_column(
        "marketplace_orders",
        sa.Column("manual_stage", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "marketplace_orders",
        sa.Column("manual_stage_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "marketplace_orders",
        sa.Column("manual_stage_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_marketplace_orders_manual_stage",
        "marketplace_orders",
        ["manual_stage"],
    )

    # Older releases treated a temporarily invisible seller offer as an owner
    # removal and disabled dumping permanently. Resume only policies whose most
    # recent run records that exact automatic suspension and which still have an
    # available priced supplier source. Explicitly disabled policies stay off.
    op.execute(
        """
        UPDATE dumping_policies
        SET enabled = true,
            updated_at = CURRENT_TIMESTAMP
        WHERE enabled = false
          AND EXISTS (
              SELECT 1
              FROM dumping_runs latest
              WHERE latest.product_id = dumping_policies.product_id
                AND latest.id = (
                    SELECT MAX(candidate.id)
                    FROM dumping_runs candidate
                    WHERE candidate.product_id = dumping_policies.product_id
                )
                AND latest.status = 'suspended_seller_removed'
          )
          AND EXISTS (
              SELECT 1
              FROM product_bindings binding
              JOIN supplier_products supplier_product
                ON supplier_product.id = binding.supplier_product_id
              LEFT JOIN supplier_offer_states offer_state
                ON offer_state.supplier_product_id = supplier_product.id
              WHERE binding.product_id = dumping_policies.product_id
                AND binding.status IN ('active', 'confirmed', 'degraded')
                AND (
                    (offer_state.price IS NOT NULL AND offer_state.available IS NOT false)
                    OR (
                        offer_state.id IS NULL
                        AND supplier_product.current_price IS NOT NULL
                        AND supplier_product.in_stock IS NOT false
                    )
                )
          )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_marketplace_orders_manual_stage", table_name="marketplace_orders")
    op.drop_column("marketplace_orders", "manual_stage_updated_at")
    op.drop_column("marketplace_orders", "manual_stage_reason")
    op.drop_column("marketplace_orders", "manual_stage")

    op.drop_index("ix_products_inventory_owner_product_id", table_name="products")
    op.drop_constraint(
        "fk_products_inventory_owner_product_id",
        "products",
        type_="foreignkey",
    )
    op.drop_column("products", "inventory_owner_product_id")
