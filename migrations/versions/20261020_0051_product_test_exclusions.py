"""Remember reviewed products and restricted brands per workspace."""
from alembic import op
import sqlalchemy as sa

revision: str = "20261020_0051"
down_revision: str | None = "20261009_0050"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_test_exclusions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("label", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "kind", "value", name="uq_product_test_exclusion"),
    )
    op.create_index("ix_product_test_exclusions_workspace_id", "product_test_exclusions", ["workspace_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE product_test_exclusions ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.drop_table("product_test_exclusions")
