"""Per-product full automation mode and durable sales experiments."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from time import sleep

revision: str = "20261006_0049"
down_revision: str | None = "20261005_0048"
branch_labels = None
depends_on = None


def _with_online_locks(change):
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        change()
        return
    # Never queue an exclusive lock behind a live worker while holding the
    # other table exclusively. NOWAIT + a savepoint releases partial locks
    # immediately, letting in-flight agent transactions finish normally.
    for attempt in range(40):
        try:
            with connection.begin_nested():
                connection.exec_driver_sql(
                    "LOCK TABLE fast_dumping_states, fast_dumping_policies "
                    "IN ACCESS EXCLUSIVE MODE NOWAIT"
                )
                change()
            return
        except DBAPIError as error:
            code = getattr(error.orig, "pgcode", None)
            if code not in {"55P03", "40P01"} or attempt == 39:
                raise
            sleep(0.25)


def _upgrade():
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


def _downgrade():
    op.drop_column("fast_dumping_states", "automation_json")
    op.drop_column("fast_dumping_policies", "automation_config")
    op.drop_column("fast_dumping_policies", "pricing_mode")


def upgrade():
    _with_online_locks(_upgrade)


def downgrade():
    _with_online_locks(_downgrade)
