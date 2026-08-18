"""Recover Fast Dumping verification and use the safe Kaspi city.

Revision ID: 20260818_0035
Revises: 20260813_0034
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260818_0035"
down_revision: str | None = "20260813_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Release products latched by the old one-shot verification behavior. A
    # fresh scan is safe because it never replays the previous write blindly.
    op.execute(
        sa.text(
            "UPDATE fast_dumping_states SET "
            "automatic_writes_paused = false, "
            "pause_reason = NULL, "
            "status = 'verification_retry', "
            "status_reason = 'Старая защитная пауза снята; ожидается новый полный scan.', "
            "next_scan_at = CURRENT_TIMESTAMP "
            "WHERE automatic_writes_paused = true "
            "AND status = 'apply_unconfirmed'"
        )
    )

    # The legacy default points Fast Dumping writes at the wrong Kaspi city for
    # this workspace and can make the offer unavailable there. Correct both
    # newly-created policies and untouched policies that still use that default.
    op.execute(
        sa.text(
            "UPDATE fast_dumping_policies "
            "SET city_id = '196220100' "
            "WHERE city_id = '750000000'"
        )
    )
    op.alter_column(
        "fast_dumping_policies",
        "city_id",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        server_default=sa.text("'196220100'"),
    )


def downgrade() -> None:
    # Preserve policy values: 196220100 may be an intentional user choice.
    op.alter_column(
        "fast_dumping_policies",
        "city_id",
        existing_type=sa.String(length=32),
        existing_nullable=False,
        server_default=sa.text("'750000000'"),
    )
