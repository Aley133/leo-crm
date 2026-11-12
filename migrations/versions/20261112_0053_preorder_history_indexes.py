"""Index latest confirmed preorder history (also safe after incident repair)."""
from alembic import op
revision: str = "20261112_0053"
down_revision: str | None = "20261020_0052"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS ix_fast_jobs_preorder_latest ON fast_dumping_jobs (workspace_id, product_id, id DESC) WHERE status = 'applied'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dumping_runs_preorder_latest ON dumping_runs (workspace_id, product_id, id DESC) WHERE published IS TRUE")

def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_dumping_runs_preorder_latest")
    op.execute("DROP INDEX IF EXISTS ix_fast_jobs_preorder_latest")
