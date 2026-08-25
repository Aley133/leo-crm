"""Lock down Supabase Data API access to the backend-only public schema.

Revision ID: 20260825_0042
Revises: 20260823_0041

LEO CRM is a backend-only application: Render talks to PostgreSQL directly via
DATABASE_URL, while owner-facing HTTP endpoints are protected separately by
SERVICE_API_TOKEN. Supabase's anon/authenticated roles therefore do not need
any direct table, sequence or function privileges.

This migration deliberately does NOT use FORCE ROW LEVEL SECURITY. The table
owner used by the backend keeps normal direct PostgreSQL access, while Data API
roles are denied by both grants and RLS.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260825_0042"
down_revision: str | None = "20260823_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _postgres_only() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres_only():
        return

    # Remove every direct Data API capability when Supabase roles exist.
    # Dynamic role checks keep local/non-Supabase PostgreSQL deployments valid.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon;
            REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon;
            REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM anon;
          END IF;

          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM authenticated;
            REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM authenticated;
            REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM authenticated;
          END IF;
        END
        $$;
        """
    )

    # Enable RLS on every existing application table, including alembic_version,
    # but do not FORCE it. The owner/backend connection therefore continues to
    # operate normally while non-owner roles require explicit policies.
    op.execute(
        """
        DO $$
        DECLARE
          table_row record;
        BEGIN
          FOR table_row IN
            SELECT schemaname, tablename
            FROM pg_tables
            WHERE schemaname = 'public'
          LOOP
            EXECUTE format(
              'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
              table_row.schemaname,
              table_row.tablename
            );
          END LOOP;
        END
        $$;
        """
    )

    # Future objects must not silently regain anon/authenticated privileges.
    # ALTER DEFAULT PRIVILEGES applies to objects created by the migration role,
    # which is the same role Render/Alembic uses for this application schema.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM anon',
              current_user
            );
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon',
              current_user
            );
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM anon',
              current_user
            );
          END IF;

          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated',
              current_user
            );
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM authenticated',
              current_user
            );
            EXECUTE format(
              'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM authenticated',
              current_user
            );
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Security hardening is intentionally fail-closed. A code rollback must not
    # silently make Supabase Data API tables public again. If direct client DB
    # access is ever introduced, privileges/policies must be added explicitly.
    return
