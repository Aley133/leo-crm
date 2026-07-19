# CTO Handoff — Phase C Monitoring Stabilization

Date: 2026-07-19  
Repository: `Aley133/leo-crm`  
Scope reviewed: monitoring core, lease engine, observation persistence, scheduler orchestration, PostgreSQL concurrency, access strategy and source health.

## Executive assessment

The project has moved from a working prototype toward a defensible platform core. PostgreSQL is the source of truth; XML, Telegram and the future Web CRM are outputs or clients. Monitoring operates on independent product bindings rather than editing XML as state.

Phase C is **not declared complete yet**. The strongest transactional and concurrency invariants are implemented, but final acceptance still requires green CI for the latest commits, PostgreSQL verification of migration `20260719_0006`, contract-document reconciliation, and the remaining explicitly open C4 items.

This document intentionally avoids numeric percentages and scores. Readiness is reported by implemented guarantees and unresolved gates rather than by subjective precision.

## Verified implementation

### 1. Transaction ownership

Application orchestration owns the accepted-result transaction.

A successful monitoring result is persisted atomically:

1. verify and lock current lease;
2. write `MonitorAttempt`;
3. lock `SupplierProduct` aggregate;
4. create or update `SupplierOfferState`;
5. append `SupplierOfferObservation` when state changes;
6. update strategy-scoped `SourceHealth`;
7. reschedule target and release lease;
8. commit once.

Failure persistence, source-health mutation and failure rescheduling are also atomic. Persistence helpers do not commit internally; older transaction-owning wrappers remain only for compatibility.

### 2. First-state concurrency

The implementation locks the parent `SupplierProduct` before reading or creating `SupplierOfferState`. This serializes the first insert and later transitions without placeholder rows.

### 3. Lease Engine

A canonical `claim_target()` defines explicit target claiming. Scheduler batch claims and manual `/run-now` use the same lease rules and claim mutation.

The engine protects against active lease theft, stale completion, paused targets, shard mismatch, premature scheduler claims and invalid lease ownership during reschedule.

### 4. Observation history

The former uniqueness constraint on `(supplier_product_id, fingerprint)` was replaced by attempt-level observation uniqueness. Repeated historical states such as `A → B → A` are stored as separate transitions.

### 5. Error-domain separation

Supplier failures are separated from persistence and configuration failures. A database failure after a successful fetch returns `persistence_error` and does not degrade supplier health. `adapter_not_registered` does not count against the supplier.

### 6. Access strategy contract

`AccessStrategy` is an application enum with persisted string values. Current values are declared in the adapter contract and include direct HTTP, public API, browser, cached, fixture and registry paths.

### 7. Enforced strategy-scoped SourceHealth

`SourceHealth` is scoped by:

```text
supplier_id + access_strategy
```

This prevents a blocked direct-HTTP path from automatically disabling a future browser path for the same supplier.

Current hard-signal policy:

- rate limit: 15 minutes;
- captcha: 1 hour;
- blocked: 6 hours;
- authentication required: 1 hour.

Before `adapter.fetch()`, scheduler reads the health row for the exact supplier/strategy pair. When the breaker is open:

- no network adapter call is made;
- no false `MonitorAttempt` is created;
- the target is deferred to `blocked_until`;
- the lease is released atomically;
- result status is `source_blocked`.

The adapter classifies evidence; the policy layer opens and enforces the breaker.

### 8. PostgreSQL concurrency proof

GitHub Actions includes a PostgreSQL 16 job in addition to SQLite unit tests. Independent PostgreSQL sessions verify:

- `FOR UPDATE SKIP LOCKED` skips locked rows;
- first state creation is serialized by the parent lock;
- a reclaimed lease rejects a stale worker without business mutations.

## Strong architectural qualities

1. PostgreSQL, not XML, is the source of truth.
2. Product, supplier product, binding, target, current state and history have distinct responsibilities.
3. Lease tokens provide fencing semantics against stale workers.
4. Meaningful state history is append-only.
5. Infrastructure failures do not poison supplier metrics.
6. Manual and scheduled monitoring share the same claim model.
7. PostgreSQL-specific behavior is tested on PostgreSQL.
8. Source health is strategy-scoped before Browser Runtime is introduced.
9. Open breaker state is enforced before network access.

## Remaining Phase C gates

### Required before Phase D is unblocked

- latest SQLite and PostgreSQL CI jobs must be green;
- migration `20260719_0006` must be tested with `alembic upgrade head` from the previous revision on PostgreSQL containing existing `source_health` data;
- monitoring contract must document strategy-scoped health and pre-fetch breaker enforcement;
- parser/schema evidence must not open a broad breaker from HTTP `403` alone;
- current migration chain must be verified against the deployed database.

### Important but separable follow-up work

- rolling statistical windows and minimum sample sizes;
- explicit `closed/open/half_open` state machine and probe ownership;
- account/profile/route scope dimensions when those concepts exist;
- stale-discard audit transaction;
- stable attempt UUID/idempotency key;
- structured logging, metrics and alerts;
- multi-worker load, pool saturation and crash-recovery tests;
- retention and rollup policy.

## Recommended next phase

Do not begin Browser Runtime until the required Phase C gates above are green.

After that, implement Browser Runtime behind the existing adapter contract in this order:

1. browser runtime interface;
2. bounded browser/context pool;
3. session/profile ownership;
4. timeouts and cancellation;
5. evidence capture for captcha/block/auth/schema failures;
6. Ozon browser adapter;
7. strategy selection and fallback policy;
8. runtime metrics and resource limits;
9. WB only after Ozon runtime is stable.

Browser Runtime must not own transactions, scheduling, pricing or source-health policy.

## Final CTO verdict

The transaction, lease, first-state serialization, historical observation and PostgreSQL concurrency work are strong and materially reduce rewrite risk. The earlier SourceHealth implementation was incomplete because it recorded breaker state without enforcing it and scoped health only by supplier. Those two defects are now addressed in code and migration `20260719_0006`, subject to CI and migration verification.

The system is not yet a finished CRM or production-scale monitor. Phase D remains blocked until the explicit acceptance gates are verified rather than inferred.
