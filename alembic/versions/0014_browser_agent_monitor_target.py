"""link browser agent jobs to monitor targets

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "browser_agent_jobs",
        sa.Column("monitor_target_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_browser_agent_jobs_monitor_target",
        "browser_agent_jobs",
        "monitor_targets",
        ["monitor_target_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_browser_agent_jobs_monitor_target_id",
        "browser_agent_jobs",
        ["monitor_target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_agent_jobs_monitor_target_id", table_name="browser_agent_jobs")
    op.drop_constraint(
        "fk_browser_agent_jobs_monitor_target",
        "browser_agent_jobs",
        type_="foreignkey",
    )
    op.drop_column("browser_agent_jobs", "monitor_target_id")
