# LEO CRM Roadmap

This roadmap is the single source of truth for delivery order and demonstrated project state. A phase is complete only when every acceptance item is implemented, migrated where necessary, and verified by automated tests.

## Current status

```text
Phase A — Foundation: verification pending
Phase B — Product and supplier core: in progress
Phase C — Monitoring stabilization: core gate verified, remaining enhancements moved to production hardening
Phase D — Marketplace Core (Kaspi-first): started
Phase E — Purchase Lifecycle Core: planned
Phase F — Browser Runtime MVP for Ozon live observations: planned
Phase G — Pricing Engine: blocked by Phase F live-data proof
Phase H — Supplier Recommendation: blocked by Phases F and G
```

The approved delivery order is defined by `docs/ADR-0004-kaspi-first-delivery-order.md`.

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
- broader API and migration tests;
- reviewed migration from generic-product Kaspi fields to marketplace-reference records.

Phase B may continue in parallel where work does not change Phase C invariants or the Phase D marketplace boundary.

## Phase C — Monitoring stabilization

Phase C stabilized the monitoring core before browser automation or automated pricing.

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
- [x] replace global historical fingerprint uniqueness with attempt-level idempotency;
- [x] verify `A -> B -> A` creates a new historical observation;
- [ ] stale worker diagnostic attempt with `result_accepted=false`;
- [ ] stable attempt idempotency key;
- [ ] MonitorDailyMetric rollup and retention.

### C3 — Adapter contract and manual vertical proof

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

Transaction and concurrency:

- [x] scheduler/application orchestrator owns the accepted-result transaction;
- [x] persistence functions never commit internally;
- [x] lock and validate MonitorTarget lease before accepted writes;
- [x] lock SupplierProduct before first SupplierOfferState creation;
- [x] lock/read SupplierOfferState before fingerprint comparison;
- [x] persist attempt, state transition, observation, reschedule and lease release atomically;
- [x] PostgreSQL first-observation race test;
- [x] PostgreSQL existing-state serialization test;
- [ ] stale results use a separate audit-only transaction and never mutate business state;
- [ ] PostgreSQL `A -> B -> A` history test.

Schema:

- [x] new immutable Alembic migration removes global observation fingerprint uniqueness;
- [x] automatic observation has attempt-level uniqueness;
- [x] no applied migration is edited;
- [ ] manual/import observation origins have an explicit future-safe identity model.

Access strategy and source health:

- [x] `AccessStrategy` is an application enum with persisted string values;
- [x] current values are explicitly declared in the adapter contract;
- [x] SourceHealth scope includes supplier and access strategy;
- [x] adapter classifies one response and returns evidence; it never opens a breaker itself;
- [x] breaker policy owns current hard-signal state transitions;
- [x] explicit hard signals may open the appropriate strategy-scoped breaker immediately;
- [x] open breaker is enforced before `adapter.fetch()`;
- [x] blocked targets resume with deterministic target-specific jitter after `blocked_until`;
- [x] per-target timeout/parse/not-found backoff remains separate from source-health state;
- [ ] account/profile/route dimensions are included when present;
- [ ] parser-schema breaker uses a reviewed statistical window;
- [ ] breaker supports explicit `closed`, `open` and `half_open` states.

C4 verified acceptance:

- [x] monitoring unit tests green for commit `329b1b6`;
- [x] PostgreSQL concurrency tests green for commit `329b1b6`;
- [x] migrations verified against PostgreSQL, including existing-data upgrade `20260719_0005 -> 20260719_0006`;
- [x] CI green for commit `329b1b6`;
- [x] architecture, monitoring contract, ORM and roadmap describe the same implemented invariants.

Moved to production-hardening gates rather than blocking Marketplace Core:

- [ ] stale-worker audit-only attempt path;
- [ ] stable execution/attempt UUID;
- [ ] richer evidence and confidence model, including safe HTTP `403` handling;
- [ ] statistical parser-schema breaker and half-open probes;
- [ ] decide whether PostgreSQL `A -> B -> A` concurrency proof is required or the existing deterministic history test is sufficient.

These items must be completed before continuous production monitoring or the full Browser Runtime is activated, but they do not block marketplace-neutral order-domain work.

## Phase D — Marketplace Core (Kaspi-first)

Kaspi is the mandatory first marketplace integration. The domain remains marketplace-neutral and follows `docs/KASPI_INTEGRATION_CONTRACT.md`.

### D0 — Architecture and contract gate

- [x] approve Kaspi-first delivery order through ADR-0004;
- [x] define Kaspi integration ownership and transaction boundaries;
- [x] define raw-payload retention and import idempotency;
- [x] separate normalized Marketplace Core from Kaspi transport DTOs;
- [ ] approve Phase D domain model and migration plan;
- [ ] define reconciliation and checkpoint model;
- [ ] define tenant/account ownership for marketplace connections.

### D1 — Marketplace domain foundation

- [ ] `MarketplaceAccount` model;
- [ ] marketplace-neutral `MarketplaceOrder` model;
- [ ] `MarketplaceOrderLine` model;
- [ ] append-only `MarketplaceOrderEvent` model;
- [ ] raw import evidence model;
- [ ] import execution/checkpoint model;
- [ ] normalized status enum with original status retained;
- [ ] immutable Alembic migration;
- [ ] unit and PostgreSQL integration tests.

### D2 — Kaspi importer vertical slice

- [ ] Kaspi transport client behind an interface;
- [ ] bounded fetch without an open business transaction;
- [ ] deterministic normalization;
- [ ] idempotent order and line upsert;
- [ ] atomic raw evidence, normalized state, event and checkpoint persistence;
- [ ] duplicate payload test;
- [ ] changed-revision test;
- [ ] unknown-status test;
- [ ] persistence rollback leaves checkpoint unchanged;
- [ ] internal outbox event after committed order changes.

### D3 — Read API and reconciliation

- [ ] list/read normalized orders;
- [ ] filter by account, status and date range;
- [ ] expose original and normalized statuses;
- [ ] show import/reconciliation errors without leaking credentials;
- [ ] manual re-import by external order identity;
- [ ] reconciliation report between external and normalized state.

## Phase E — Purchase Lifecycle Core

This phase implements deterministic purchasing lifecycle only. Supplier recommendation is explicitly excluded.

- [ ] approved purchase domain model;
- [ ] purchase request linked to an originating order or manual demand;
- [ ] versioned lifecycle transitions;
- [ ] ordered, partially received, received, cancelled and closed states;
- [ ] receipt records and audit events;
- [ ] idempotent downstream handling of marketplace-order events;
- [ ] no automatic supplier selection;
- [ ] no dependency on raw Kaspi payloads;
- [ ] transaction and concurrency tests.

## Phase F — Browser Runtime MVP for Ozon live observations

Browser Runtime is infrastructure and must reuse the normalized monitoring adapter result.

MVP scope:

- [ ] separate worker process, never the web process;
- [ ] one Ozon browser access strategy;
- [ ] bounded timeout and concurrency;
- [ ] one managed authenticated or anonymous session profile as required;
- [ ] normalized live price, availability and delivery evidence;
- [ ] captcha/block evidence capture;
- [ ] no pricing, purchase or XML writes from Browser Runtime;
- [ ] operational kill switch;
- [ ] representative live-evidence contract tests;
- [ ] continuous production use remains blocked until Phase C hardening gates are satisfied.

The full browser pool, multi-profile lifecycle, remote browser strategies and broad anti-bot support are later runtime expansions, not MVP requirements.

## Phase G — Pricing Engine

Pricing may begin only after Phase F proves a live Ozon observation path.

- [ ] Money value object;
- [ ] immutable PriceCalculation;
- [ ] calculation idempotency key;
- [ ] optimistic ProductPriceState update;
- [ ] manual override priority;
- [ ] pricing failure does not erase an accepted observation;
- [ ] every calculation stores an explainable breakdown;
- [ ] real Ozon observations included in acceptance tests;
- [ ] commission, delivery, tax, floor and minimum-margin policies are versioned.

## Phase H — Supplier Recommendation

Recommendation consumes accepted supplier observations and explainable pricing results. It is not part of Purchase Lifecycle or Browser Runtime.

- [ ] recommendation policy contract;
- [ ] compare supplier price, delivery, availability and confidence;
- [ ] deterministic tie-breaking;
- [ ] recommendation audit trail;
- [ ] manual override and rejection reasons;
- [ ] no automatic purchase in the initial release;
- [ ] acceptance tests use live-shaped Ozon data and deterministic fixtures.

## Phase I — XML publication

- [ ] immutable XML versions;
- [ ] validation before activation;
- [ ] explicit input PriceStateVersion set;
- [ ] stale generation rejection;
- [ ] atomic active-version switch;
- [ ] stable `/feeds/kaspi.xml` endpoint;
- [ ] retention and storage thresholds.

## Phase J — Production worker activation

Continuous production monitoring may not be enabled until:

- [ ] an always-on paid Render worker or equivalent host is provisioned;
- [ ] worker heartbeat monitoring is active;
- [ ] source-health and queue/backlog alerts are active;
- [ ] production connection budget is rechecked for API plus worker processes;
- [ ] source-specific emergency stop is available;
- [ ] runbook for captcha, block, migration and rollback incidents is documented;
- [ ] remaining Phase C production-hardening gates are complete.

## Phase K — Telegram operations interface

Telegram is the first operational interface, but remains an authenticated API client with no database, monitoring, pricing or purchasing logic.

## Phase L — Web CRM

Dashboard, product cards, binding review, orders, purchasing, monitoring status, exception handling and audit views.

## Phase M — Inventory, finance and analytics expansion

Warehouse movements, FIFO, realized margin, forecasting, automation and carefully scoped automatic actions require separate approved domain models.

## Working rules

1. PostgreSQL is the source of truth. XML, Telegram and Web CRM are outputs or clients.
2. The Domain Model gate applies to new high-risk modules, not routine stabilization.
3. No phase is marked complete from a successful deployment alone.
4. Every completed roadmap item requires code, migration where applicable, and automated verification.
5. Roadmap changes are part of the definition of done for the commit that changes an invariant.
6. Roadmap changes are allowed when implementation or CTO review proves an assumption false.
7. A public deployment must fail closed when authentication configuration is missing.
8. Once an Alembic migration has been applied in a shared or production environment, it is immutable.
9. Critical state transitions use explicit transaction ownership at the application-orchestrator level.
10. Adapters classify external evidence; policy layers make platform-wide decisions.
11. Browser Runtime never owns marketplace, monitoring, pricing, purchase or XML business state.
12. Pricing and supplier recommendation require a proven live supplier observation path.
13. Purchase Lifecycle and Supplier Recommendation are separate responsibilities.
14. New Phase D code must not deepen the existing coupling between generic Product and Kaspi identifiers.
