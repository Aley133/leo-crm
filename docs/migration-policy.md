# Database Migration Policy

Status: Active engineering rule

## 1. Source of truth

All persistent schema changes are made through Alembic migrations. ORM metadata, the canonical domain contract, and Alembic must describe the same persisted model before a gate is approved.

## 2. Immutability rule

Once a migration revision has been applied in any shared, staging, or production environment, its file is immutable.

Corrections must be introduced in a new revision with a new `revision` identifier and the affected revision as `down_revision`.

Editing an already applied migration is allowed only during an explicitly declared recovery incident when:

- the deployment failed before the revision was recorded in `alembic_version`;
- the database is left in a partially applied state;
- no later migration depends on the affected revision;
- the final intended schema is unchanged;
- the recovery and reason are documented in the commit message.

The idempotent repair to `20260718_0004` was such a recovery incident. It is not a precedent for normal development.

## 3. Retry safety

Production migrations should be retry-safe where practical, especially when external deployment systems can interrupt a build. Retry safety does not replace migration immutability.

## 4. Deployment rule

A release is accepted only when:

1. migrations finish successfully;
2. the application starts against the migrated schema;
3. health checks pass;
4. automated contract tests are green.

Continuous production monitoring must not start while a schema revision is pending or failed.

## 5. Test requirements

Before Phase C production activation, CI must verify:

- the Alembic revision chain has one head;
- upgrade from baseline to head succeeds on PostgreSQL;
- downgrade/upgrade smoke coverage exists for newly introduced Phase C revisions where safe;
- persisted constraints match the canonical contract.
