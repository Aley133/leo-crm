# Monitoring Contract v0.1

Status: Draft for implementation review

This contract defines the minimum data and runtime behavior required before Phase C monitoring code is accepted.

## 1. MonitorTarget

Purpose: current scheduling and lease state for one independently monitored supplier card.

Required fields:

```json
{
  "id": 101,
  "product_binding_id": 55,
  "supplier_product_id": 9001,
  "status": "active",
  "next_check_at": "2026-07-18T18:00:00Z",
  "check_interval_seconds": 300,
  "priority": 100,
  "bucket": 1,
  "lease_owner": null,
  "lease_token": null,
  "lease_until": null,
  "consecutive_failures": 0,
  "last_checked_at": null,
  "last_success_at": null,
  "last_error_code": null,
  "source_health_status": "healthy",
  "created_at": "2026-07-18T17:00:00Z",
  "updated_at": "2026-07-18T17:00:00Z"
}
```

Claim rule:

```sql
SELECT id
FROM monitor_targets
WHERE status = 'active'
  AND next_check_at <= now()
  AND (lease_until IS NULL OR lease_until < now())
ORDER BY priority DESC, next_check_at ASC
FOR UPDATE SKIP LOCKED
LIMIT :limit;
```

A claim writes a new cryptographically random `lease_token`. All later writes that reschedule or release the target must match both `id` and `lease_token`. A stale worker may record its attempt result, but it may not overwrite the current target lease or schedule.

## 2. MonitorAttempt

Purpose: short-lived diagnostic record for every claimed execution.

Required fields:

```json
{
  "id": 70001,
  "monitor_target_id": 101,
  "lease_token": "uuid",
  "adapter_code": "ozon_browser_v1",
  "access_strategy": "browser",
  "started_at": "2026-07-18T18:00:01Z",
  "finished_at": "2026-07-18T18:00:04Z",
  "duration_ms": 3100,
  "outcome": "success",
  "error_class": null,
  "http_status": 200,
  "captcha_detected": false,
  "rate_limited": false,
  "request_fingerprint": "sha256:...",
  "observation_id": 88001,
  "retry_number": 0,
  "diagnostic_metadata": {},
  "created_at": "2026-07-18T18:00:04Z"
}
```

Retention: raw attempts are retained for 14 days initially. Before deletion they are aggregated into `MonitorDailyMetric`.

## 3. SupplierOfferState

Purpose: one current row containing the latest accepted normalized facts for a supplier card.

Required fields:

```json
{
  "supplier_product_id": 9001,
  "price": 5640,
  "old_price": 6200,
  "currency": "KZT",
  "availability": "in_stock",
  "stock_quantity": null,
  "delivery_days": 2,
  "delivery_text": "22 июля",
  "seller_name": "Example seller",
  "business_fingerprint": "sha256:...",
  "adapter_schema_version": 1,
  "observed_at": "2026-07-18T18:00:04Z",
  "last_checked_at": "2026-07-18T18:00:04Z",
  "version": 12
}
```

An unchanged check updates `last_checked_at` but does not create another business observation.

## 4. SupplierOfferObservation

Purpose: append-only record created when normalized business facts change or when policy explicitly requires a diagnostic observation.

Required fields:

```json
{
  "id": 88001,
  "supplier_product_id": 9001,
  "previous_state_version": 11,
  "new_state_version": 12,
  "business_fingerprint": "sha256:...",
  "price": 5640,
  "availability": "in_stock",
  "stock_quantity": null,
  "delivery_days": 2,
  "seller_name": "Example seller",
  "change_mask": ["price", "delivery_days"],
  "source_observed_at": "2026-07-18T18:00:03Z",
  "committed_at": "2026-07-18T18:00:04Z"
}
```

Required uniqueness:

```text
supplier_product_id + business_fingerprint + adapter_schema_version
```

## 5. Error classification

Allowed top-level classes:

```text
not_found
out_of_stock
rate_limited
captcha_required
auth_required
source_blocked
timeout
network_error
parser_schema_changed
invalid_response
database_unavailable
pricing_failed
internal_error
```

`captcha_required`, `source_blocked`, `rate_limited`, `timeout` and `parser_schema_changed` must never be translated to product absence.

## 6. SourceHealth

Allowed states:

```text
healthy
degraded
rate_limited
captcha_required
auth_required
blocked
disabled
```

Repeated source-wide failures open a circuit breaker. While open, targets are rescheduled according to source policy instead of repeatedly hammering the platform.

## 7. Workflow transaction contract

### A. Claim transaction

- claim due target;
- generate lease token;
- commit;
- close database session.

### B. External request

- execute without holding a database connection;
- apply adapter timeout and bounded retries;
- normalize the response;
- calculate deterministic business fingerprint.

### C. Observation transaction

- insert `MonitorAttempt` result;
- upsert `SupplierOfferState`;
- insert `SupplierOfferObservation` only when facts changed;
- commit regardless of later pricing success;
- close database session.

### D. Pricing transaction

- calculate from committed observation and policy version;
- use a deterministic idempotency key;
- insert or reuse `PriceCalculation`;
- update `ProductPriceState` with optimistic locking;
- commit;
- close database session.

### E. Reschedule transaction

- update target only when lease token still matches;
- calculate next check time and failure counters;
- clear lease;
- commit.

## 8. Initial concurrency contract

```text
network_semaphore = 8
observation_write_semaphore = 2
pricing_write_semaphore = 2
outbox_write_semaphore = 1
```

External requests may run concurrently up to the network limit. Database phases must use their own semaphore and must not hold a connection while waiting for a network semaphore.

## 9. Adapter acceptance cases

Every automatic adapter must pass deterministic mocked tests for:

- valid 200 response;
- card not found;
- out of stock;
- HTTP 429 / rate limit;
- captcha page;
- authentication required;
- timeout;
- malformed response;
- parser schema change;
- identical business facts with changed HTML formatting;
- meaningful price or delivery change.

An adapter that cannot classify these cases is manual-review-only and cannot drive automatic pricing.
