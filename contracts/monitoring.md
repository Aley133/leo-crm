# Monitoring Contract v0.2

Status: Canonical implementation contract for Phase C.

This file must match the ORM models and Alembic schema. Future fields may be added only through an architecture change, migration, ORM update and contract update in the same work item.

## 1. MonitorTarget

Purpose: current schedule and lease state for one ProductBinding.

Persisted fields:

```json
{
  "id": 101,
  "product_binding_id": 55,
  "status": "active",
  "interval_seconds": 300,
  "next_check_at": "2026-07-18T18:00:00Z",
  "last_checked_at": null,
  "consecutive_failures": 0,
  "lease_owner": null,
  "lease_token": null,
  "lease_until": null,
  "shard": 0,
  "created_at": "2026-07-18T17:00:00Z",
  "updated_at": "2026-07-18T17:00:00Z"
}
```

Allowed statuses:

```text
active
paused
degraded
manual_review
disabled
```

Claim selection:

```sql
SELECT id
FROM monitor_targets
WHERE status = 'active'
  AND next_check_at <= now()
  AND (lease_until IS NULL OR lease_until < now())
ORDER BY next_check_at ASC
FOR UPDATE SKIP LOCKED
LIMIT :limit;
```

A claim writes a cryptographically random `lease_token`, `lease_owner` and `lease_until`. Every release or reschedule must match both target id and lease token. A stale worker may create a diagnostic attempt but may not mutate the current lease or schedule.

Priority currently belongs to ProductBinding. Source-health status is read from SourceHealth and is not duplicated into MonitorTarget.

## 2. MonitorAttempt

Purpose: short-lived diagnostic record for one claimed execution.

Persisted fields:

```json
{
  "id": 70001,
  "monitor_target_id": 101,
  "lease_token": "opaque-random-token",
  "outcome": "success",
  "adapter_code": "ozon_browser_v1",
  "access_strategy": "browser",
  "started_at": "2026-07-18T18:00:01Z",
  "finished_at": "2026-07-18T18:00:04Z",
  "duration_ms": 3100,
  "http_status": 200,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-07-18T18:00:04Z"
}
```

Allowed outcomes:

```text
success
timeout
rate_limited
captcha
blocked
auth_required
not_found
parse_error
network_error
internal_error
```

Detailed conditions are represented by `outcome`, `error_code` and `error_message`. Fields such as `captcha_detected`, `request_fingerprint`, `observation_id`, `retry_number` and diagnostic JSON are not part of the current persisted contract. They may be introduced later only with a migration and contract revision.

Retention: raw attempts are retained for 14 days initially. Before deletion, operational reliability is aggregated into MonitorDailyMetric when that model is introduced in C2.

## 3. SupplierOfferState

Purpose: the single current normalized state row for one SupplierProduct.

Persisted fields:

```json
{
  "id": 5001,
  "supplier_product_id": 9001,
  "price": "5640.00",
  "old_price": "6200.00",
  "available": true,
  "stock": null,
  "delivery_days": 2,
  "seller": "Example seller",
  "fingerprint": "64-character-sha256",
  "adapter_schema_version": "ozon-v1",
  "observed_at": "2026-07-18T18:00:04Z",
  "last_checked_at": "2026-07-18T18:00:04Z",
  "version": 12,
  "updated_at": "2026-07-18T18:00:04Z"
}
```

Current Phase C assumes supplier prices are normalized into the business pricing currency before persistence. A dedicated Money/currency model is introduced with Pricing in C4. `delivery_text` is adapter diagnostic input and is not part of current normalized state.

An unchanged check updates `last_checked_at` but does not create another SupplierOfferObservation.

## 4. SupplierOfferObservation

Purpose: append-only significant observation of normalized supplier facts.

Persisted fields:

```json
{
  "id": 88001,
  "supplier_product_id": 9001,
  "monitor_attempt_id": 70001,
  "price": "5640.00",
  "old_price": "6200.00",
  "available": true,
  "stock": null,
  "delivery_days": 2,
  "seller": "Example seller",
  "fingerprint": "64-character-sha256",
  "adapter_schema_version": "ozon-v1",
  "raw_metadata": null,
  "observed_at": "2026-07-18T18:00:03Z",
  "created_at": "2026-07-18T18:00:04Z"
}
```

Required uniqueness in the current schema:

```text
supplier_product_id + fingerprint
```

The fingerprint itself includes `adapter_schema_version`, therefore an adapter schema change produces a different deterministic fingerprint.

## 5. Fingerprint contract

Every automatic adapter must produce a deterministic SHA-256 fingerprint from normalized business facts:

```text
supplier_product_id
price
available
stock
delivery_days
normalized seller
adapter_schema_version
```

HTML formatting, whitespace and casing changes that do not change normalized business facts must not create a new fingerprint.

## 6. Error classification

MonitorAttempt outcomes are the persisted top-level classification. Recommended error codes provide finer detail, for example:

```text
out_of_stock
captcha_required
source_blocked
parser_schema_changed
invalid_response
database_unavailable
pricing_failed
```

`captcha`, `blocked`, `rate_limited`, `timeout`, `auth_required` and `parse_error` must never be translated to product absence. `not_found` means the adapter positively identified that the external card does not exist.

## 7. SourceHealth

Persisted fields:

```json
{
  "id": 1,
  "supplier_id": 1,
  "status": "healthy",
  "consecutive_failures": 0,
  "blocked_until": null,
  "last_success_at": null,
  "last_failure_at": null,
  "last_error_code": null,
  "updated_at": "2026-07-18T18:00:04Z"
}
```

Allowed states:

```text
healthy
degraded
rate_limited
captcha_required
blocked
auth_required
disabled
```

Repeated source-wide failures open a circuit breaker. While open, targets are rescheduled according to source policy instead of repeatedly calling the platform.

## 8. Workflow transaction contract

### A. Claim transaction

- claim due target;
- generate lease token;
- write owner and expiry;
- commit;
- close database session.

### B. External request

- execute without holding a database connection;
- apply adapter timeout and bounded retries;
- normalize response;
- calculate deterministic fingerprint.

### C. Observation transaction

- insert or complete MonitorAttempt;
- upsert SupplierOfferState;
- insert SupplierOfferObservation only when facts changed;
- commit independently of later pricing success;
- close database session.

### D. Pricing transaction

- calculate from committed observation and policy version;
- use deterministic idempotency key;
- insert or reuse PriceCalculation;
- update ProductPriceState with optimistic locking;
- commit;
- close database session.

### E. Reschedule transaction

- update target only when lease token still matches;
- calculate next-check time and failure counters;
- clear lease;
- commit.

## 9. Initial concurrency contract

```text
network_semaphore = 8
observation_write_semaphore = 2
pricing_write_semaphore = 2
outbox_write_semaphore = 1
API/worker database pool_size = 2
API/worker max_overflow = 1
```

External requests may use the network semaphore. Database phases use separate bounded semaphores and must not hold a connection while waiting for network work.

## 10. Adapter acceptance cases

Every automatic adapter must pass deterministic mocked tests for:

- valid 200 response;
- card not found;
- out of stock;
- HTTP 429;
- captcha;
- authentication required;
- timeout;
- malformed response;
- parser schema change;
- identical business facts with changed HTML formatting;
- meaningful price, availability or delivery change.

An adapter that cannot classify these cases is manual-review-only and cannot drive automatic pricing.
