# Monitoring Execution Contract

Status: canonical

This document defines the runtime invariants of LEO CRM monitoring. Code, ORM, migrations, tests, API paths and the roadmap must remain consistent with this contract.

## 1. Ownership boundaries

- PostgreSQL is the source of truth.
- XML, Telegram and Web CRM are clients or generated outputs.
- Supplier adapters only access an external source and classify evidence.
- Adapters do not own transactions, scheduling, pricing or circuit-breaker policy.
- The scheduler/application orchestrator owns the accepted-result transaction.
- Internal persistence helpers flush but never commit or roll back the caller-owned transaction.

## 2. Lease and fencing rules

A monitor target may be processed only after a successful lease claim.

The lease consists of:

- `lease_owner`;
- cryptographically random `lease_token`;
- `lease_until`.

The token is a fencing token. Every accepted write must validate and lock the current `MonitorTarget` row using the target id and the current lease token.

A stale worker must not:

- mutate `SupplierOfferState`;
- append `SupplierOfferObservation`;
- reschedule the target;
- release the current worker's lease.

Batch scheduler claims use `FOR UPDATE SKIP LOCKED`. Manual `/run-now` and scheduler execution share the canonical claim rules through `claim_target()` / the common claim mutation.

## 3. Accepted success transaction

A successful adapter result is accepted in one transaction:

1. lock and validate the current `MonitorTarget` lease;
2. write `MonitorAttempt`;
3. lock the parent `SupplierProduct` aggregate;
4. lock/read or create `SupplierOfferState`;
5. compare the new fingerprint with current state;
6. update current state;
7. append `SupplierOfferObservation` only for a meaningful state transition;
8. update strategy-scoped `SourceHealth` as successful;
9. reschedule the target and release its lease;
10. commit once.

If any step fails, the entire accepted-result transaction is rolled back.

## 4. Failure transaction

A real adapter/source failure is persisted atomically with target backoff:

1. validate the current lease;
2. write failed `MonitorAttempt`;
3. update strategy-scoped `SourceHealth` using classified evidence;
4. apply per-target failure reschedule/backoff;
5. release the lease;
6. commit once.

Infrastructure and persistence failures are not supplier failures. A database failure after a successful fetch must return `persistence_error` and must not degrade `SourceHealth`.

Configuration failures such as an unregistered adapter may be recorded on the target attempt but do not count against supplier health.

## 5. Observation history and idempotency

`SupplierOfferState` is the latest accepted state.

`SupplierOfferObservation` is append-only meaningful history.

History must preserve repeated transitions:

```text
A -> B -> A
```

The final `A` is a new observation, not a reference to the first historical row.

Global uniqueness by `(supplier_product_id, fingerprint)` is forbidden. Automatic observation idempotency is tied to `monitor_attempt_id`.

## 6. First-state concurrency

An absent child row cannot be protected by a child-row lock. Therefore the first `SupplierOfferState` creation is serialized by locking the parent `SupplierProduct` before reading or creating the state row.

This avoids:

- duplicate first-state rows;
- placeholder business-state rows;
- first-insert `IntegrityError` races.

## 7. Access strategy

Access strategy is an explicit application contract persisted as a string value. Current declared strategies are:

- `direct_http`;
- `public_api`;
- `browser`;
- `cached`;
- `fixture`;
- `registry`.

Runtime orchestration must pass the actual adapter strategy explicitly. `direct_http` is allowed only as a compatibility default for legacy internal callers and migrated rows.

## 8. SourceHealth scope and breaker enforcement

`SourceHealth` is scoped by:

```text
supplier_id + access_strategy
```

A breaker for `direct_http` must not automatically block `browser` for the same supplier.

Current hard-signal policy may open a bounded breaker immediately for:

- rate limiting;
- captcha;
- explicit blocking evidence;
- authentication requirement.

The adapter classifies evidence. The SourceHealth policy decides state and duration.

Before any `adapter.fetch()` call, orchestration must:

1. resolve the adapter and its access strategy;
2. load `SourceHealth` for `(supplier_id, access_strategy)`;
3. check `blocked_until`;
4. when blocked, skip external access, release the lease and return `source_blocked`;
5. set `next_check_at` to `blocked_until` plus a deterministic target-specific recovery offset.

A blocked-source deferral must not create a `MonitorAttempt`, because no external attempt occurred.

### Deterministic recovery scheduling

Targets sharing one breaker must not all become due at the exact same instant. Recovery scheduling therefore adds a stable per-target offset in the range 1–180 seconds after `blocked_until`.

The offset must:

- be derived from `target_id`;
- remain stable across retries and process restarts;
- avoid runtime randomness and random seeds;
- be bounded;
- distribute sequential target ids across the recovery window.

This staggered recovery prevents a thundering herd immediately after the breaker expires while preserving reproducible scheduling and tests.

Per-target timeout/parse/not-found backoff remains separate from strategy-scoped SourceHealth.

## 9. Timestamp rule

Runtime timestamps are UTC-aware. SQLite test persistence may return naive timestamps even for timezone-aware columns; comparison helpers must normalize these values without changing PostgreSQL semantics.

## 10. Migration rules

- Applied/shared Alembic revisions are immutable.
- Schema changes require a new revision.
- Revision metadata must follow the repository's typed Alembic format.
- The chain must remain linear.
- Migration changes affecting existing rows require a PostgreSQL upgrade/downgrade smoke test.

Relevant current revisions:

- `20260719_0005`: observation idempotency and repeatable history;
- `20260719_0006`: SourceHealth scope by supplier and access strategy.

## 11. Required automated proof

The monitoring gate requires automated tests for:

- transaction rollback boundaries;
- stale lease rejection;
- `FOR UPDATE SKIP LOCKED` on PostgreSQL;
- first-state creation concurrency;
- existing-state serialization;
- repeated `A -> B -> A` history;
- strategy-scoped SourceHealth;
- breaker enforcement before network access;
- deterministic, bounded and distributed recovery jitter;
- migration chain linearity;
- PostgreSQL migration of existing SourceHealth data from `0005` to `0006`.

## 12. Explicitly not complete yet

The current contract does not claim completion of:

- stale-worker audit-only attempts;
- stable execution/attempt UUID before adapter access;
- account/profile/proxy/route health dimensions;
- rolling statistical parser-schema breaker;
- explicit `closed/open/half_open` breaker state machine;
- production observability, alerting and load testing.

Browser Runtime may not weaken any invariant in this document and must remain infrastructure behind the adapter contract.
