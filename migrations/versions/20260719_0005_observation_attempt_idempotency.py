"""Replace global observation fingerprint uniqueness with attempt idempotency.

Revision ID: 20260719_0005
Revises: 20260718_0004
Create Date: 2026-07-19
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260719_0005"
down_revision: str | None = "20260718_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_supplier_observation_fingerprint",
        "supplier_offer_observations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_supplier_observation_attempt",
        "supplier_offer_observations",
        ["monitor_attempt_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_supplier_observation_attempt",
        "supplier_offer_observations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_supplier_observation_fingerprint",
        "supplier_offer_observations",
        ["supplier_product_id", "fingerprint"],
    )
