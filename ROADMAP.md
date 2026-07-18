# LEO CRM Roadmap

This roadmap reports demonstrated project state. A phase is complete only when every acceptance item is implemented and verified.

## Current status

```text
Phase A — Foundation: in verification
Phase B — Product and supplier core: in progress
Phase C — Monitoring reliability vertical slice: design approved, implementation gated
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
- canonical monitoring contract v0.2;
- CI test workflow;
- service-token protection for private `/api/*` endpoints;
- explicit SQLAlchemy pool limits (`pool_size=2`, `max_overflow=1`).

Still required before marking fully complete:

- configure `SERVICE_API_TOKEN` in Render and verify authenticated/unauthenticated requests;
- verify the current GitHub Actions run is green;
- add future Supabase JWT authentication for the web CRM when the web client starts.

## Phase B — Product and supplier core

Completed:

- Product model and create/list API;
- Supplier model and create/list API;
- SupplierProduct model and create API;
- ProductBinding model and create API;
- binding lifecycle fields;
- database migrations through monitoring schema foundation;
- service authentication usable by Swagger and the future Telegram adapter.

In progress / missing:

- list/read/update endpoints for SupplierProduct;
- list/read/update and lifecycle-transition endpoints for ProductBinding;
- validation of allowed binding transitions;
- primary-binding uniqueness policy;
- Telegram API client skeleton;
- import of existing TGBAD bindings;
- automated API and migration tests beyond current contract tests.

Phase B is not complete until the above items are verified.

## Phase C — Monitoring reliability vertical slice

### C0 — Domain and schema gate

- [x] architecture contract;
- [x] Phase C domain model;
- [x] monitoring contract reconciled with ORM and persisted schema;
- [x] initial monitoring migration;
- [x] production migration through `20260718_0004` deployed;
- [x] database constraints for current invariants;
- [x] explicit connection-pool limits;
- [ ] add reusable test database fixture;
- [ ] add migration upgrade/downgrade smoke test against PostgreSQL;
- [ ] verify all C0 contract tests in green CI.

### C1 — Lease Engine

- [ ] claim due targets with `FOR UPDATE SKIP LOCKED`;
- [ ] create cryptographically random LeaseToken;
- [ ] prevent stale-token mutation;
- [ ] release lease;
- [ ] success reschedule;
- [ ] failure reschedule with backoff;
- [ ] expired lease recovery;
- [ ] concurrency tests.

### C2 — Observation Engine

- [ ] MonitorAttempt lifecycle;
- [ ] normalized adapter result;
- [ ] mandatory fingerprint;
- [ ] current SupplierOfferState update;
- [ ] append-only significant observations;
- [ ] unchanged-result deduplication;
- [ ] source error classification;
- [ ] MonitorDailyMetric rollup and retention.

### C3 — Ozon adapter contract

- [ ] adapter interface;
- [ ] mocked 200 response;
- [ ] mocked 404 response;
- [ ] mocked 429 response;
- [ ] mocked captcha response;
- [ ] mocked timeout;
- [ ] SourceHealth transition tests;
- [ ] circuit breaker.

### C4 — Pricing integration

- [ ] Money value object;
- [ ] immutable PriceCalculation;
- [ ] calculation idempotency key;
- [ ] optimistic ProductPriceState update;
- [ ] manual override priority;
- [ ] pricing failure does not erase observation.

### C5 — XML publication

- [ ] immutable XML versions;
- [ ] validation before activation;
- [ ] explicit input PriceStateVersion set;
- [ ] stale generation rejection;
- [ ] atomic active-version switch;
- [ ] stable `/feeds/kaspi.xml` endpoint;
- [ ] retention and storage thresholds.

### Phase C production infrastructure gate

The Lease Engine and adapters may be developed and tested on the current free Render plan. Continuous production monitoring may not be enabled until:

- [ ] an always-on paid Render worker or equivalent always-on host is provisioned;
- [ ] worker heartbeat monitoring is active;
- [ ] source and outbox backlog alerts are active;
- [ ] the production connection budget is rechecked for API plus worker processes.

## Later phases

### Phase D — Telegram operations interface

Telegram is the first operational interface but remains an API client.

### Phase E — Web CRM

Dashboard, product cards, binding review, monitoring status and error handling.

### Phase F — Orders, purchasing and inventory

Requires a separate domain model before implementation.

### Phase G — Analytics and automation expansion

Forecasting, supplier selection, purchasing recommendations and carefully scoped automatic actions.

## Working rules

1. The Domain Model gate applies to new high-risk Phase C modules, not to Phase B fixes and stabilization.
2. No phase is marked complete from a successful deployment alone.
3. Every completed roadmap item requires code, migration where applicable, and automated tests.
4. Roadmap changes are allowed when real implementation reveals a false assumption.
5. A public deployment must fail closed when authentication configuration is missing.
