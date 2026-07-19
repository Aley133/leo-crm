# Kaspi Integration Contract

Status: Initial approved contract for Phase D

## 1. Purpose

Kaspi is the mandatory first marketplace integration and the operational entry point for the first commercial release. This contract keeps Kaspi transport details outside Marketplace Core while preserving every external fact needed for audit, replay and reconciliation.

## 2. Ownership

Kaspi Integration owns:

- authentication and request construction;
- external pagination and checkpoints;
- raw payload capture;
- Kaspi status and identifier translation;
- transport retries and bounded timeouts;
- detection of malformed or incomplete responses.

Marketplace Core owns:

- normalized orders and order lines;
- lifecycle invariants;
- idempotent import application;
- business events;
- links to purchases, inventory and finance;
- reconciliation state.

Kaspi Integration must not own purchasing, pricing, inventory write-offs or profit calculation.

## 3. Required normalized entities

### MarketplaceAccount

Represents one connected seller account.

Required identity:

- internal account id;
- marketplace code (`kaspi`);
- external merchant or partner identity;
- tenant or organization identity;
- active/inactive state.

### MarketplaceOrder

Required fields:

- internal id;
- marketplace account id;
- marketplace code;
- external order id;
- external order number when distinct;
- normalized status;
- order creation time with source timezone retained;
- normalized currency;
- normalized gross amount;
- customer-facing delivery mode where available;
- destination city or location where available;
- source revision or update timestamp where available;
- created and updated timestamps.

The unique business identity is:

```text
marketplace_account_id + external_order_id
```

### MarketplaceOrderLine

Required fields:

- internal id;
- order id;
- external line id when available;
- merchant SKU when available;
- marketplace product reference when available;
- normalized title snapshot;
- quantity;
- unit gross amount;
- line gross amount;
- normalized currency.

Line identity must prefer a stable external line id. When Kaspi does not provide one, the integration must derive and version a deterministic fallback identity from stable source fields. The fallback algorithm must be documented before production activation.

### MarketplaceOrderEvent

Append-only event recording a meaningful external or internal lifecycle transition.

Required fields:

- order id;
- event type;
- normalized previous and next status where applicable;
- external source timestamp where available;
- import execution id;
- raw payload reference;
- created timestamp.

## 4. Raw payload retention

Every accepted import execution must retain enough source evidence to explain the normalized result.

Raw payload storage must include:

- marketplace account id;
- request scope or page cursor;
- fetched timestamp;
- response hash;
- payload or durable object reference;
- adapter schema version;
- import execution id.

Raw payloads are evidence, not the source of truth for current business state. Normalized database entities are the operational source of truth.

Sensitive credentials, authorization headers and secret tokens must never be stored in raw payload records.

## 5. Import execution and idempotency

Every polling or manual import run receives a stable `import_execution_id`.

An imported order is applied idempotently by:

```text
marketplace_account_id
+ external_order_id
+ source_revision_or_payload_hash
```

Repeated delivery of the same external state must not:

- create a duplicate order;
- duplicate order lines;
- duplicate the same business event;
- trigger a second purchase request;
- repeat an inventory or finance mutation.

A later source revision may update current normalized state and append a new order event.

## 6. Fetch and transaction boundary

Network calls must not hold database transactions.

Canonical flow:

```text
1. Read connection settings and checkpoint.
2. Fetch Kaspi data without an open business transaction.
3. Validate and normalize outside the transaction.
4. Open one short application-owned transaction.
5. Lock the marketplace account/import checkpoint when required.
6. Persist raw import metadata, normalized changes, events and checkpoint atomically.
7. Commit once.
```

Adapters and persistence helpers never commit internally.

## 7. Status mapping

Kaspi statuses must be translated through an explicit versioned mapping into a limited core lifecycle.

Initial core lifecycle categories:

- `new`;
- `accepted`;
- `processing`;
- `ready_for_shipment`;
- `shipped`;
- `delivered`;
- `cancelled`;
- `returned`;
- `unknown`.

The original Kaspi status must always be retained alongside the normalized value.

Unknown external statuses must not be silently coerced into a known business state. They are stored as `unknown`, recorded as evidence and surfaced for review.

## 8. Downstream event boundary

An accepted normalized order change may publish an internal event through an outbox after the importing transaction commits.

Examples:

- `marketplace_order.created`;
- `marketplace_order.status_changed`;
- `marketplace_order.delivered`;
- `marketplace_order.cancelled`.

Purchase Lifecycle consumes these internal events. It must not consume raw Kaspi payloads directly.

## 9. Product identity boundary

A generic `Product` must not require a Kaspi identifier as its primary identity.

Kaspi product codes and merchant offer identifiers belong in marketplace-reference records linked to the generic product. Existing schema fields may remain temporarily for compatibility, but new Phase D code must not deepen that coupling. Migration requires a separate reviewed plan with backfill and rollback tests.

## 10. Failure behavior

A failed fetch or malformed page must not partially advance the import checkpoint.

A normalization error must:

- preserve evidence;
- identify the affected external record;
- avoid mutating the normalized order;
- remain retryable;
- not classify a Kaspi business status as a supplier-source breaker event.

## 11. Phase D initial acceptance gate

Before the first Kaspi importer is considered complete:

- domain entities contain no Kaspi transport DTOs;
- repeated identical payload import is idempotent;
- a status change appends exactly one event;
- unknown status is preserved and surfaced;
- order and lines persist atomically;
- checkpoint does not advance on failed persistence;
- network fetch occurs without an open database transaction;
- raw payload evidence contains no credentials;
- tests cover duplicate delivery, changed revision, malformed order and rollback.
