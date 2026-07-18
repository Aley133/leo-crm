# LEO CRM Architecture v0.3 Addendum

Status: Approved for Phase B. Phase C remains blocked until the monitoring contract and approval tests below are implemented.

This addendum resolves the remaining ambiguities identified during architecture review. Where this document conflicts with `docs/architecture.md`, this addendum takes precedence until both documents are merged.

## 1. Deterministic XML activation rule

XML publication uses immutable versioned files and one stable public endpoint.

Generation contract:

1. The generator reads an explicit set of `ProductPriceState.version` values and stores them as the generation input manifest.
2. It renders a new immutable file, for example `feed_000126.xml`.
3. The file is fully written and validated before any database activation step begins.
4. Immediately before activation, the service re-reads every relevant `ProductPriceState.version`.
5. If any version differs from the input manifest, the generated file is marked `stale`, is never activated, and a replacement generation is scheduled.
6. If all versions still match, one short database transaction updates `XmlFeed.active_version_id` to the validated version.
7. If that transaction rolls back, the previous `active_version_id` remains active.
8. `GET /feeds/kaspi.xml` serves only the file referenced by the committed active version. It never streams a file that is still being written.

There is no alternative activation branch. Stale input always means reject and regenerate.

## 2. Pricing idempotency

`PriceCalculation` must not be duplicated when the same supplier observation is processed repeatedly.

Required fields:

- `observation_id`;
- `pricing_policy_version`;
- `input_fingerprint`;
- `idempotency_key`;
- `result_price`;
- `calculation_explanation`;
- `created_at`.

The canonical idempotency key is derived from:

```text
product_id
+ observation_id
+ pricing_policy_version
+ manual_override_version
```

The database enforces uniqueness on `idempotency_key`.

Repeated execution returns the existing `PriceCalculation` instead of inserting another business-equivalent record. `ProductPriceState` still uses optimistic locking through its `version` column.

## 3. Observation fingerprints

A deterministic fingerprint is mandatory for every adapter and every access strategy used in Phase C.

The fingerprint is calculated from normalized business facts, not raw HTML and not the observation timestamp:

```text
supplier_product_id
+ normalized price
+ normalized availability
+ normalized stock
+ normalized delivery days
+ normalized seller identity
+ adapter schema version
```

Rules:

- API, browser and manual adapters must produce the same canonical business representation where they observe the same facts.
- Raw response metadata may differ and is not part of the business fingerprint.
- A timestamp bucket is not accepted as the primary deduplication strategy.
- If an adapter cannot produce the required normalized fields, it is not approved for automatic monitoring and must degrade to manual review.

## 4. Concurrency and database pools

Network concurrency and database-write concurrency use separate limits.

Initial Worker limits:

```text
network_semaphore = 8
observation_write_semaphore = 2
pricing_write_semaphore = 2
outbox_write_semaphore = 1
```

Initial Worker SQLAlchemy pool:

```text
pool_size = 2
max_overflow = 1
```

Rules:

- Network tasks do not hold database connections.
- A task must acquire the relevant database-phase semaphore before opening a session for transaction C, D or E.
- Total concurrent database-writing phases must remain within the tested pool capacity.
- Semaphore values are configuration, but a deployment is rejected if configured database-phase concurrency exceeds the supported connection budget.
- Pool checkout wait time and saturation are operational metrics.

## 5. Phase C approval gate additions

The existing approval gate is extended with these mandatory tests.

### Test 9: deterministic XML activation

Given a generation that starts from price-state version `N`, when the price-state version changes to `N+1` before activation:

- the generated XML version is marked stale;
- `active_version_id` is unchanged;
- the stale file is never served by `/feeds/kaspi.xml`;
- a replacement generation is scheduled.

Also verify that a database rollback during activation leaves the previous active version available.

### Test 10: idempotent pricing

Given one `SupplierOfferObservation`, when the pricing workflow processes it twice:

- only one business-equivalent `PriceCalculation` exists;
- both executions resolve to the same idempotency key;
- `ProductPriceState.version` does not advance twice for the same result;
- the audit trail records a retry without creating a duplicate calculation.

### Test 11: adapter fingerprint contract

For every approved Phase C adapter and access strategy:

- identical normalized facts produce identical fingerprints;
- a meaningful price, availability, stock, delivery or seller change produces a different fingerprint;
- raw HTML formatting changes alone do not create a new business observation.

### Test 12: concurrency versus pool capacity

Under configured network concurrency:

- database phases stay within configured write semaphores;
- pool checkout latency remains below the accepted threshold;
- a slow external request does not block outbox dispatch;
- no task keeps a database connection during the external request.

## 6. Final approval statement

Phase B is approved.

Phase C implementation may begin only after:

- `contracts/monitoring.md` is accepted;
- tests 1-12 from the complete Phase C approval gate exist as executable tests or deterministic integration-test specifications;
- the first Ozon adapter passes mocked cases for success, not found, rate limit, captcha, timeout and parser-schema change.
