# CTO Handoff — Phase C Monitoring Stabilization

Date: 2026-07-19
Repository: `Aley133/leo-crm`
Scope reviewed: monitoring core, lease engine, observation persistence, scheduler orchestration, PostgreSQL concurrency, source health.

## Executive assessment

The project has moved from a working prototype toward a defensible platform core. The main architectural direction is sound: PostgreSQL is the source of truth, XML and Telegram are interfaces, and monitoring operates on independent product bindings rather than editing XML as state.

Phase C should be considered substantially complete, but not production-complete. The monitoring core is ready to support the next phase, Browser Runtime, provided the remaining operational controls are added before high-volume production traffic.

Overall engineering assessment for the current phase: **8.8/10**.

Estimated completion:

- Phase C Monitoring Stabilization: **96%**
- Entire planned platform: **20–25%**
- Production readiness of monitoring subsystem: **70–75%**

The difference between Phase C completion and production readiness is intentional: architecture and concurrency are now strong, while observability, deployment controls, retention policy, load testing and operational recovery still need work.

## What was completed

### 1. Transaction ownership

Application orchestration now owns the transaction boundary.

A successful monitoring result is persisted atomically:

1. verify and lock current lease;
2. write `MonitorAttempt`;
3. lock `SupplierProduct` aggregate;
4. create or update `SupplierOfferState`;
5. append `SupplierOfferObservation` when state changes;
6. update `SourceHealth`;
7. reschedule target and release lease;
8. commit once.

Failure persistence and failure rescheduling are also atomic.

Persistence helpers no longer commit internally. Backward-compatible transaction-owning wrappers remain for older call sites.

### 2. First-state concurrency

The existing child-row lock protected updates to an existing `SupplierOfferState`, but could not protect the first insert because no row existed.

The implementation now locks the parent `SupplierProduct` before reading or creating state. This serializes both first creation and later transitions without introducing empty placeholder state rows.

### 3. Lease Engine

A canonical `claim_target()` now defines explicit target claiming. Scheduler batch claims and manual `/run-now` use the same lease rules and the same claim mutation.

The engine protects against:

- active lease theft;
- stale worker completion;
- paused/non-active targets;
- shard mismatch;
- premature scheduler claims;
- invalid lease ownership during reschedule.

### 4. Observation history

The former unique constraint on `(supplier_product_id, fingerprint)` incorrectly collapsed repeated historical states.

The model now allows:

`A → B → A`

as three separate historical observations.

Idempotency is tied to the monitoring attempt rather than global historical fingerprint uniqueness.

### 5. Error-domain separation

Adapter/source failures are separated from persistence/infrastructure failures.

A database failure after a successful supplier fetch now returns `persistence_error` and does not create a false supplier failure or degrade source health.

Configuration failures such as `adapter_not_registered` are recorded on the target attempt but do not count against supplier health.

### 6. Access strategy contract

Access strategy is now a typed contract with initial values:

- `direct_http`
- `public_api`
- `browser`
- `cached`
- `fixture`
- `registry`

Adapters may remain string-compatible during migration, but scheduler persistence normalizes the value.

### 7. Source Health

`SourceHealth` now has transactional business logic rather than being only a table.

Current policy:

- success restores `healthy`, resets consecutive failures and clears a breaker;
- three ordinary consecutive failures set `degraded`;
- hard signals open a bounded source-level breaker immediately:
  - rate limit: 15 minutes;
  - captcha: 1 hour;
  - blocked: 6 hours;
  - authentication required: 1 hour.

The adapter classifies evidence; policy code decides the health state and breaker duration.

### 8. PostgreSQL concurrency proof

GitHub Actions now includes a real PostgreSQL 16 job in addition to the fast SQLite test job.

Real independent PostgreSQL sessions prove:

- `FOR UPDATE SKIP LOCKED` skips a row held by another transaction;
- first state creation is serialized by the parent lock;
- a reclaimed lease rejects the stale worker without attempts, state changes, observations or schedule mutation.

## Strong architectural qualities

1. **Correct source of truth** — PostgreSQL, not XML.
2. **Clear aggregate boundaries** — product, supplier product, binding, target, state and history are distinct.
3. **Fencing-token semantics** — lease token prevents stale workers from mutating state.
4. **Append-only meaningful history** — state transitions remain auditable.
5. **Infrastructure isolation** — database failures do not poison supplier metrics.
6. **Shared orchestration path** — manual and scheduled monitoring no longer drift apart.
7. **Database-specific testing** — concurrency claims are tested on PostgreSQL rather than inferred from SQLite.
8. **Forward compatibility** — Browser Runtime can plug into the adapter/access-strategy contract without rewriting the monitoring core.

## Remaining risks and recommended priorities

### Priority 1 — Circuit-breaker enforcement

`SourceHealth` records bounded breaker state, but scheduler claim/fetch flow must still enforce it before network access. The next implementation should decide the scope explicitly:

- supplier-wide;
- supplier + access strategy;
- optionally region/account/proxy later.

A supplier-wide breaker alone may be too coarse once direct HTTP and browser access coexist.

### Priority 2 — Statistical health windows

Current ordinary degradation uses consecutive failures. Before large-scale production, add rolling-window statistics, for example:

- minimum sample size;
- failure ratio over last N attempts;
- parser/schema error ratio;
- hard-signal override;
- half-open probe behavior.

Do not replace hard signals with statistics; use both.

### Priority 3 — Stale attempt audit path

Current stale workers correctly create no business mutations. A separate idempotent audit transaction for `stale_discarded` remains useful for diagnosis, but it should not weaken the accepted-result transaction.

### Priority 4 — Attempt idempotency key

Observation idempotency is tied to `monitor_attempt_id`, but externally retryable execution would benefit from a stable attempt UUID/idempotency key created before adapter execution.

### Priority 5 — Operational observability

Before production scale, add:

- structured logs with target, supplier, lease and attempt identifiers;
- metrics for claim latency, adapter duration, persistence errors and stale discards;
- alerts for breaker openings and schema-break ratios;
- trace/correlation IDs across scheduler, adapter and persistence.

### Priority 6 — Load and recovery testing

Required before tens of thousands of monitored products:

- multi-worker load test;
- database pool saturation test;
- worker crash between claim and completion;
- lease-expiry recovery test under load;
- migration upgrade test against a database containing real historical rows.

### Priority 7 — Migration deployment verification

The observation uniqueness migration must be applied and verified in the deployed PostgreSQL environment. CI model creation is not a substitute for testing `alembic upgrade head` from the actual previous revision.

## Recommended next phase

Proceed to **Phase D — Browser Runtime**, but implement it as infrastructure behind the existing adapter contract.

Recommended order:

1. browser runtime interface;
2. bounded browser/context pool;
3. session/profile ownership;
4. timeouts and cancellation;
5. evidence capture for captcha/block/auth/schema failures;
6. Ozon browser adapter;
7. strategy selection and fallback policy;
8. runtime metrics and resource limits;
9. WB adapter only after Ozon runtime is stable.

Browser Runtime must not own monitoring transactions, scheduling, pricing or source-health policy.

## Final CTO verdict

The architecture no longer shows signs of an inevitable rewrite. The most important monitoring invariants are explicit and tested. The work completed in Phase C materially reduced the risk of duplicate processing, stale writes, partial persistence and corrupted historical data.

The system is ready for the next engineering phase, but it is not yet a finished CRM or a production-scale monitoring service. The correct next investment is Browser Runtime plus operational controls—not UI expansion, XML features or pricing complexity.
