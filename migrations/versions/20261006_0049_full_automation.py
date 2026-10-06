"""Per-product full automation mode and durable sales experiments."""

from alembic import op
import sqlalchemy as sa

revision: str = "20261006_0049"
down_revision: str | None = "20261005_0048"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "fast_dumping_policies",
        sa.Column(
            "pricing_mode", sa.String(32), nullable=False, server_default="manual"
        ),
    )
    op.add_column(
        "fast_dumping_policies",
        sa.Column("automation_config", sa.JSON(), nullable=True),
    )
    op.add_column(
        "fast_dumping_states", sa.Column("automation_json", sa.JSON(), nullable=True)
    )


def downgrade():
    op.drop_column("fast_dumping_states", "automation_json")
    op.drop_column("fast_dumping_policies", "automation_config")
    op.drop_column("fast_dumping_policies", "pricing_mode")
