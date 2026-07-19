"""add browser agent job queue

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_agent_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_product_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_payload", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("lease_token", name="uq_browser_agent_jobs_lease_token"),
    )
    op.create_index("ix_browser_agent_jobs_supplier_product_id", "browser_agent_jobs", ["supplier_product_id"])
    op.create_index("ix_browser_agent_jobs_status", "browser_agent_jobs", ["status"])
    op.create_index("ix_browser_agent_jobs_lease_until", "browser_agent_jobs", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_browser_agent_jobs_lease_until", table_name="browser_agent_jobs")
    op.drop_index("ix_browser_agent_jobs_status", table_name="browser_agent_jobs")
    op.drop_index("ix_browser_agent_jobs_supplier_product_id", table_name="browser_agent_jobs")
    op.drop_table("browser_agent_jobs")
