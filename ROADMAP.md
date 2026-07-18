# LEO CRM Roadmap

This roadmap reports demonstrated project state. A phase is complete only when every acceptance item is implemented and verified.

## Current status

```text
Phase A — Foundation: mostly complete
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
- monitoring contracts.

Still required before marking fully complete:

- CI test workflow;
- authentication implementation, not only plan;
- explicit database pool configuration in deployed runtime.

## Phase B — Product and supplier core

Completed:

- Product model and create/list API;
- Supplier model and create/list API;
- SupplierProduct model and create API;
- ProductBinding model and create API;
- binding lifecycle fields;
- database migrations through monitoring schema foundation.

In progress / missing:

- list/read/update endpoints for SupplierProduct;
- list/read/update and lifecycle-transition endpoints for ProductBinding;
- validation of allowed binding transitions;
- primary-binding uniqueness policy;
- Telegram API client skeleton;
- import of existing TGBAD bindings;
- automated API and migration tests;
- service authentication for Telegram.

Phase B is not complete until the above items are verified.

## Phase C — Monitoring reliability vertical slice

### C0 — Domain and schema gate

- [x] architecture contract;
- [x] Phase C domain model;
- [x] monitoring JSON contract;
- [x] initial monitoring migration;
- [ ] verify migration on production database;
- [ ] reconcile ORM models with migration;
- [ ] add database constraints for all invariants;
- [ ] add test database fixture.

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
