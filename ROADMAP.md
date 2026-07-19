# LEO CRM Roadmap

This roadmap is the single source of truth for delivery order and demonstrated project state. A phase is complete only when every acceptance item is implemented, migrated where necessary, and verified by automated tests.

## Current status

```text
Phase A — Foundation: verification pending
Phase B — Product and supplier core: in progress
Phase C — Monitoring stabilization: core gate verified, remaining enhancements in progress
Phase D — Browser access runtime: blocked by remaining Phase C requirements
```

## Phase A — Foundation

Completed:

- GitHub repository;
- Render deployment;
- FastAPI application;
- Supabase PostgreSQL connection;
- Alembic migration baseline;
- `/health` endpoint;
- Swagger documentation;
- architecture v0.2 and Phase C addendum;
- canonical monitoring contract at `docs/MONITORING_CONTRACT.md`;
- CI test workflow;
- service-token protection for private `/api/*` endpoints;
- explicit SQLAlchemy pool limits (`pool_size=2`, `max_overflow=1`) for PostgreSQL;
- dialect-aware SQLite test-engine configuration;
- documented immutable migration policy.

Still required before marking fully complete:

- configure and verify `SERVICE_API_TOKEN` in Render;
- verify authenticated requests return `200` and unauthenticated requests return `401`;
- verify the current GitHub Actions run is green;
- add Supabase JWT authentication when the Web CRM client starts.

## Phase B — Product and supplier core

Completed:

- Product model and create/list API;
- Supplier model and create/list API;
- SupplierProduct model and create API;
- ProductBinding model and create API;
- binding lifecycle fields;
- database migrations through the monitoring schema foundation;
- service authentication usable by Swagger and future clients.

In progress / missing:

- list/read/update endpoints for SupplierProduct;
- list/read/update and lifecycle-transition endpoints for ProductBinding;
- validation of allowed binding transitions;
- primary-binding uniqueness policy;
- Telegram API client skeleton;
- import of existing TGBAD bindings;
- broader API and migration tests.

Phase B may continue in parallel where work does not change Phase C invariants.

## Phase C — Monitoring stabilization

Phase C stabilizes the monitoring core before any browser automation is introduced.

### C0 — Schema and architecture gate

- [x] architecture contract;
- [x] Phase C domain model;
- [x] monitoring contract reconciled with ORM and persisted schema;
- [x] initial monitoring migration;
- [x] production migrations through `20260718_0004` deployed;
- [x] explicit PostgreSQL connection-pool limits;
- [x] SQLite CI compatibility for engine creation;
- [x] migration immutability and recovery policy documented;
- [ ] reusable test database fixture;
- [x] PostgreSQL migration upgrade/downgrade smoke test;
- [x] all current C0 contract tests verified in green CI.

### C1 — Lease and scheduler foundation

Implemented, final verification pending:

- [x] claim due targets with `FOR UPDATE SKIP LOCKED`;
- [x] cryptographically random lease token;
- [x] stale-token mutation protection;
- [x] lease release;
- [x] success reschedule;
- [x] failure reschedule with per-target backoff;
- [x] expired lease recovery;
- [x] scheduler orchestration;
- [x] intentional `/run-now` path;
- [x] shared `claim_target()` primitive for scheduler and `/run-now`;
- [x] real PostgreSQL lease-claim concurrency test.

### C2 — Observation and current-state engine

Implemented, stabilization pending:

- [x] MonitorAttempt lifecycle;
- [x] normalized adapter result;
- [x] mandatory business fingerprint;
- [x] current SupplierOfferState update;
- [x] append-only significant observations;
- [x] unchanged-result deduplication against current state;
- [x] row lock for an existing SupplierOfferState;
- [x] source error classification foundation;
- [x] protect first-state creation through a locked SupplierProduct aggregate root;
- [x] accepted observation and success reschedule committed in one orchestrator-owned transaction;
- [ ] stale worker diagnostic attempt with `result_accepted=false`;
- [ ] stable attempt idempotency key;
- [x] replace global historical fingerprint uniqueness with attempt-level idempotency;
- [x] verify `A -> B -> A` creates a new historical observation;
- [ ] MonitorDailyMetric rollup and retention.

### C3 — Adapter contract and manual vertical proof

Implemented, reliability classification pending:

- [x] adapter interface;
- [x] Ozon direct-HTTP adapter foundation;
- [x] mocked success/not-found/rate-limit/captcha/timeout paths;
- [x] scheduler-to-adapter vertical path;
- [x] manual run-now proof path;
- [ ] richer adapter failure evidence;
- [ ] scope hints for route, account/profile and source;
- [ ] explicit confidence and evidence codes;
- [ ] verify that HTTP `403` is not treated as a source-wide block from status code alone.

### C4 — Monitoring stabilization gate

C4 must be complete before Browser Runtime starts.

Transaction and concurrency:

- [x] scheduler/application orchestrator owns the accepted-result transaction;
- [x] persistence functions never commit internally;
- [x] lock and validate MonitorTarget lease before accepted writes;
- [x] lock SupplierProduct before first SupplierOfferState creation;
- [x] lock/read SupplierOfferState before fingerprint comparison;
- [x] persist attempt, state transition, observation, reschedule and lease release atomically;
- [ ] stale results use a separate audit-only transaction and never mutate business state;
- [x] PostgreSQL first-observation race test;
- [x] PostgreSQL existing-state serialization test;
- [ ] PostgreSQL `A -> B -> A` history test.

Schema:

- [x] new immutable Alembic migration removes global observation fingerprint uniqueness;
- [x] automatic observation has attempt-level uniqueness;
- [ ] manual/import observation origins have an explicit future-safe identity model;
- [x] no applied migration is edited.

Access strategy and source health:

- [x] `AccessStrategy` is an application enum with persisted string values;
- [x] current values are explicitly declared in the adapter contract;
- [x] SourceHealth scope includes supplier and access strategy;
- [ ] account/profile/route dimensions are included when present;
- [x] adapter classifies one response and returns evidence; it never opens a breaker itself;
- [x] breaker policy owns current hard-signal state transitions;
- [x] explicit hard signals may open the appropriate strategy-scoped breaker immediately;
- [x] open breaker is enforced before `adapter.fetch()` and defers the target until `blocked_until`;
- [ ] parser-schema breaker requires a 15-minute window, at least 20 attempts, at least 10 distinct previously healthy targets and at least 40% classified failures;
- [ ] breaker supports explicit `closed`, `open` and `half_open` states;
- [x] per-target timeout/parse/not-found backoff remains separate from source-health state.

C4 acceptance:

- [x] all monitoring unit tests green for the latest implementation commit;
- [x] all PostgreSQL concurrency tests green for the latest implementation commit;
- [x] migrations verified against PostgreSQL, including existing-data upgrade `20260719_0005 -> 20260719_0006`;
- [x] CI green for the latest implementation commit;
- [x] architecture, monitoring contract, ORM and roadmap describe the same implemented invariants.

Remaining Phase C requirements before Phase D:

- [ ] stale-worker audit-only attempt path;
- [ ] stable execution/attempt UUID;
- [ ] richer evidence and confidence model, including safe HTTP `403` handling;
- [ ] statistical parser-schema breaker and half-open probes;
- [ ] decide whether PostgreSQL `A -> B -> A` concurrency proof is required or the existing deterministic history test is sufficient.

## Phase D — Browser access runtime

Blocked until the remaining Phase C requirements are explicitly completed or formally moved to a later production gate by an architecture decision.

- [ ] separate worker process, never the web process;
- [ ] browser profile lifecycle;
- [ ] authenticated session handling;
- [ ] per-strategy concurrency limits;
- [ ] bounded retries and timeouts;
- [ ] captcha/block evidence capture;
- [ ] normalized adapter result identical to other access strategies;
- [ ] no pricing or XML writes from Browser Runtime;
- [ ] operational kill switch.

## Phase E — Pricing integration

- [ ] Money value object;
- [ ] immutable PriceCalculation;
- [ ] calculation idempotency key;
- [ ] optimistic ProductPriceState update;
- [ ] manual override priority;
- [ ] pricing failure does not erase an accepted observation;
- [ ] every calculation stores an explainable breakdown.

## Phase F — XML publication

- [ ] immutable XML versions;
- [ ] validation before activation;
- [ ] explicit input PriceStateVersion set;
- [ ] stale generation rejection;
- [ ] atomic active-version switch;
- [ ] stable `/feeds/kaspi.xml` endpoint;
- [ ] retention and storage thresholds.

## Phase G — Production worker activation

Continuous production monitoring may not be enabled until:

- [ ] an always-on paid Render worker or equivalent host is provisioned;
- [ ] worker heartbeat monitoring is active;
- [ ] source-health and queue/backlog alerts are active;
- [ ] production connection budget is rechecked for API plus worker processes;
- [ ] source-specific emergency stop is available;
- [ ] runbook for captcha, block, migration and rollback incidents is documented.

## Phase H — Telegram operations interface

Telegram is the first operational interface, but remains an authenticated API client with no database, monitoring or pricing logic.

## Phase I — Web CRM

Dashboard, product cards, binding review, monitoring status, exception handling and audit views.

## Phase J — Orders, purchasing and inventory

Requires separate approved domain models. Order polling may reuse the scheduler and lease infrastructure, while business ownership remains inside the Orders module.

## Phase K — Analytics and automation expansion

Forecasting, supplier selection, purchasing recommendations and carefully scoped automatic actions.

## Working rules

1. PostgreSQL is the source of truth. XML, Telegram and Web CRM are outputs or clients.
2. The Domain Model gate applies to new high-risk modules, not routine Phase B stabilization.
3. No phase is marked complete from a successful deployment alone.
4. Every completed roadmap item requires code, migration where applicable, and automated verification.
5. Roadmap changes are part of the definition of done for the commit that changes an invariant.
6. Roadmap changes are allowed when implementation proves an assumption false.
7. A public deployment must fail closed when authentication configuration is missing.
8. Once an Alembic migration has been applied in a shared or production environment, it is immutable.
9. Critical state transitions use explicit transaction ownership at the application-orchestrator level.
10. Adapters classify external evidence; policy layers make platform-wide decisions.
11. Browser automation cannot begin until the monitoring stabilization gate is green.
