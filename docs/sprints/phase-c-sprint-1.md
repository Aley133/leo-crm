# Phase C — Sprint 1

Goal: prove the monitoring lease and claim contract before any real supplier request is executed.

## Sprint structure

Sprint 1 is split into two sequential gates. Gate 2 does not start until Gate 1 is reviewed.

## Gate 1 — Domain and schema validation

Deliverables:

- review `docs/domain-model-phase-c.md` against `docs/architecture.md`;
- confirm canonical event names;
- verify migration `20260718_0003` on PostgreSQL;
- reconcile every migration column with ORM models;
- add database constraints for binding statuses, monitor statuses and source-health statuses;
- define test database configuration;
- add migration smoke test.

Acceptance criteria:

1. No domain document contradicts transaction boundaries A-E.
2. No duplicate event name exists for the same business fact.
3. ORM metadata and Alembic schema describe the same tables and constraints.
4. Existing Product, Supplier and ProductBinding data survive upgrade and downgrade rehearsal in a disposable database.
5. Phase B endpoints still pass smoke tests.

## Gate 2 — Lease Engine

Deliverables:

- `claim_due_targets` application service;
- PostgreSQL implementation using `FOR UPDATE SKIP LOCKED`;
- LeaseToken generation;
- release/reschedule service;
- expired-lease recovery;
- concurrency and stale-token tests.

Required behaviour:

```text
claim:
  select active due targets
  ignore valid leases
  lock rows with SKIP LOCKED
  assign owner, token and lease_until
  commit quickly

external work:
  happens after commit
  holds no DB connection

complete:
  update only where id and lease_token match
  stale token changes zero rows
```

Acceptance tests:

1. Two workers cannot successfully claim the same target generation.
2. A stale worker cannot release a newly claimed target.
3. An expired lease becomes claimable again.
4. A successful completion resets consecutive failures and advances next_check_at.
5. A failed completion increments failures and applies policy backoff.
6. Claim transactions do not include external HTTP work.
7. Claim batch size and DB-write concurrency remain bounded.

## Explicit non-goals

Sprint 1 does not include:

- real Ozon requests;
- browser automation;
- pricing formulas;
- XML generation;
- Telegram notifications;
- automatic product matching.

## Definition of done

Sprint 1 is complete only when Gate 1 and Gate 2 tests pass in CI and the migration is verified against the deployed database. A Swagger screen alone is not evidence of completion.
