# LEO CRM Architecture v0.2

Status: Approved for Phase B; Phase C remains blocked until the monitoring contract is implemented.
Owner: Business owner
Technical direction: Full-stack / architecture

## 1. Product vision

LEO CRM is a private operating system for the owner's marketplace business. It is not a public SaaS and is not designed for generic sellers.

The target state is a machine that minimizes manual participation in:

- product monitoring;
- supplier search and binding;
- price and delivery tracking;
- safe repricing;
- XML generation and publication;
- order processing;
- purchasing;
- inventory receipt and movement;
- cancellations, expenses, margin and profit analytics;
- notifications and exception handling.

Telegram is the primary operational interface of the MVP, but it is not the system core. Telegram and the future web interface are clients of the same authenticated API.

## 2. Core architectural principles

1. PostgreSQL is the single source of truth.
2. XML is generated output, never the primary storage.
3. Every Kaspi product is an independent business entity.
4. One product may have multiple supplier cards and multiple supplier platforms.
5. Monitoring, pricing, XML, orders and purchasing are separate modules inside one modular monolith.
6. No module reads or writes another module's ORM models or repositories directly. Cross-module work uses a public application-service or query interface.
7. We do not create ceremonial interfaces for every function. Boundaries are enforced where they protect business ownership or allow replacement of an external adapter.
8. Important history is immutable. Current state and historical observations are stored separately.
9. Background work runs outside the API request lifecycle.
10. Telegram and the website communicate only through the API.
11. Secrets exist only in deployment environment variables.
12. Database structure changes only through Alembic migrations.
13. Critical operational workflows favor explicit transaction boundaries over architectural purity.
14. Every automatic decision must be explainable and recoverable.
15. External-source failures are expected operating states, not exceptional surprises.

## 3. System context

```text
                          Kaspi XML consumer
                                  ^
                                  |
                         /feeds/kaspi.xml
                                  ^
                                  |
Telegram Bot -----> Authenticated LEO CRM API <----- Web CRM
                                  |
                                  v
                      Application service layer
                                  |
                                  v
                             PostgreSQL
                                  ^
                                  |
                     Worker runtime / adapters
                                  ^
                                  |
               Ozon / WB / Kaspi / other sources
```

Telegram and Web CRM never connect directly to PostgreSQL.

## 4. Main bounded modules

### 4.1 Catalog

Purpose: represent our own marketplace products.

Main entities:

- Product
- ProductStatus
- ProductAttribute
- ProductExternalIdentifier

Product lifecycle:

```text
draft -> active -> paused -> archived
```

- `draft`: incomplete or not approved for automation;
- `active`: eligible for monitoring, pricing and XML;
- `paused`: temporarily excluded from automatic operations;
- `archived`: retired while history remains available.

Catalog does not parse suppliers and does not calculate prices.

### 4.2 Suppliers

Purpose: represent external platforms and concrete supplier cards.

Main entities:

- Supplier
- SupplierAccount
- SupplierProduct
- SupplierOfferState
- SupplierOfferObservation
- SourceHealth

`SupplierOfferState` stores the latest known state, one row per supplier product:

- current price and old price;
- availability and stock;
- delivery text and normalized delivery days;
- seller;
- last successful observation;
- last checked timestamp;
- current parser/access status;
- state version.

`SupplierOfferObservation` is append-only and is written only for meaningful changes or diagnostic evidence. A successful check with no business change updates `SupplierOfferState.last_checked_at` without creating a full duplicate snapshot.

Required index:

```text
(supplier_product_id, observed_at DESC)
```

Raw payload retention is bounded. Large raw responses are stored in object storage or discarded after the diagnostic window.

### 4.3 Matching and bindings

Purpose: connect a Product with one or more SupplierProduct records.

Main entities:

- ProductBinding
- MatchCandidate
- MatchDecision

Binding lifecycle:

```text
candidate -> confirmed -> active -> degraded -> disabled
       \-> rejected
```

The matching engine may propose candidates. It may not silently replace a confirmed binding. Promotion to `active` requires confirmation or an explicitly approved high-confidence rule.

### 4.4 Monitoring and external-source reliability

Purpose: independently schedule and execute checks for every active supplier binding.

Main entities:

- MonitorTarget
- MonitorPolicy
- MonitorAttempt
- MonitorDailyMetric
- SourceHealth

There is no persistent `MonitorJob` queue in the first version.

`MonitorTarget` stores current scheduling state:

- binding_id;
- next_check_at;
- last_checked_at;
- interval_seconds;
- priority;
- bucket;
- consecutive_failures;
- lease_owner;
- lease_until;
- current degradation state.

Workers claim targets using a short transaction:

```sql
SELECT ...
FROM monitor_targets
WHERE next_check_at <= now()
  AND (lease_until IS NULL OR lease_until < now())
ORDER BY priority DESC, next_check_at
FOR UPDATE SKIP LOCKED
LIMIT :batch_size;
```

Lease duration must cover:

```text
adapter timeout
+ configured retries
+ observation persistence budget
+ pricing workflow budget
+ safety margin
```

A lease heartbeat may extend the lease for long operations. Completion and rescheduling use a claim token so an expired worker cannot overwrite a newer worker's result.

`MonitorAttempt` is a short-lived diagnostic journal, retained for 7-30 days. It records:

- target and attempt identifiers;
- adapter and access strategy;
- start/end and duration;
- HTTP/result classification;
- captcha, block, timeout or parser error;
- retry count;
- lease owner;
- error details with bounded payload size.

Before raw attempts are deleted, a daily rollup is persisted in `MonitorDailyMetric`:

- attempts;
- successes;
- failures;
- captcha count;
- blocked count;
- timeout count;
- average and percentile latency;
- longest success gap.

This preserves quarterly and annual reliability analytics after raw diagnostic retention expires.

#### Source-health states

```text
healthy
-> degraded
-> rate_limited
-> captcha_required
-> blocked
-> auth_required
-> disabled
```

The adapter must distinguish at least:

- product not found;
- product out of stock;
- source timeout;
- HTTP 429/rate limit;
- captcha;
- IP or account block;
- authentication required;
- parser/layout break;
- transport failure.

Repeated captchas or blocks do not trigger endless fast retries. The target enters degraded/manual-review state and the source-level circuit breaker increases backoff.

`AccessStrategy` is adapter-specific and may include:

- official API;
- authenticated browser session;
- browser profile and cookies;
- direct HTTP;
- proxy route where legally and operationally justified;
- manual verification fallback.

Proxy infrastructure is not added before measured failure data justifies it. Terms-of-service and legal risk must be reviewed before this system is offered to third parties.

### 4.5 Pricing

Purpose: calculate a safe sale price and dynamic floor.

Main entities:

- PricingPolicy
- ProductPricingPolicy
- PriceCalculation
- ProductPriceState

Every calculation stores a complete explanation:

```text
supplier price
+ supplier delivery
+ marketplace commission
+ payment cost
+ tax
+ fixed costs
+ risk buffer
+ target profit
= safe sale price
```

`ProductPriceState` includes:

- version for optimistic locking;
- last calculation_id;
- source observation_id;
- updated_by;
- update_reason;
- manual_override type/value;
- manual_override_until;
- current floor and sale price.

Priority order:

```text
sale prohibition
> manual lock
> manual fixed price/floor
> automatic calculation
```

Automatic writes use optimistic locking. A stale calculation may not overwrite a newer manual or automatic decision.

### 4.6 XML

Purpose: generate and publish Kaspi-compatible XML.

Main entities:

- XmlFeed
- XmlFeedVersion
- XmlOfferState
- XmlGenerationRun

XML files are immutable and versioned:

```text
feed_000123.xml
feed_000124.xml
feed_000125.xml
```

Generation flow:

```text
read an explicit pricing-state version
-> generate a new immutable file
-> validate structure and required offers
-> persist file and checksum
-> atomically update XmlFeed.active_version_id
```

Kaspi always uses one stable endpoint:

```text
GET /feeds/kaspi.xml
```

The endpoint serves the active valid version itself. It does not depend on marketplace support for HTTP redirects.

The previous valid version remains active if generation or validation fails.

Storage policy:

- keep the active version;
- keep a configurable number of recent valid versions;
- keep failed-generation metadata, not unlimited failed files;
- delete old files only after confirming they are not active;
- alert before storage reaches configured soft and hard limits.

### 4.7 Orders

Purpose: import and maintain the complete Kaspi order lifecycle.

Kaspi order import is treated as another scheduled polling task. It reuses the same scheduling, lease, retry, backoff and execution infrastructure as supplier monitoring, while its business handler remains inside the Orders module.

Main entities:

- Order
- OrderItem
- OrderStatusHistory
- Cancellation
- Fulfillment

Imports are idempotent by Kaspi order identifier and preserve bounded raw source data for diagnosis.

### 4.8 Purchasing

The first version recommends and tracks purchases. It does not automatically complete checkout on an external marketplace.

Main entities:

- PurchaseNeed
- PurchaseOrder
- PurchaseOrderItem
- PurchaseStatusHistory
- Receipt
- ReceiptItem

### 4.9 Inventory

Inventory uses an append-only stock-movement ledger.

Main entities:

- Warehouse
- StockItem
- StockMovement
- Reservation
- Batch

Current stock is a projection derived from movements. Corrections are new movements, never destructive edits of history.

### 4.10 Finance and analytics

Main entities:

- CostEntry
- RevenueEntry
- ProfitCalculation
- ProductDailyMetric
- SupplierMetric
- MonitorDailyMetric

Financial calculations must be reproducible from source records. Long-term monitoring reliability is calculated from daily rollups, not only from raw MonitorAttempt rows.

### 4.11 Notifications and Telegram

Telegram is the production-ready owner interface of the MVP. It is a client of the backend and contains no pricing, monitoring or database logic.

Initial Telegram capabilities:

- confirm or reject binding candidates;
- pause/resume a product;
- request a priority recheck;
- view monitoring degradation;
- receive price-below-floor alerts;
- view supplier disappearance or captcha/block alerts;
- view XML publication failures;
- view purchase recommendations.

### 4.12 Audit and outbox

Main entities:

- BusinessEvent
- AuditLog
- OutboxEvent

The first version uses a transactional outbox for secondary reactions:

- Telegram notifications;
- analytics projections;
- audit integrations;
- non-critical follow-up work.

The critical `observation -> pricing decision` workflow does not wait for the outbox dispatcher.

The Outbox Dispatcher is an explicit asynchronous loop inside the Worker runtime. It claims rows using `FOR UPDATE SKIP LOCKED` and provides at-least-once delivery.

Outbox fields include:

- event_id and idempotency key;
- event type and payload version;
- created_at;
- attempts;
- next_attempt_at;
- locked_by and locked_until;
- processed_at;
- last_error;
- dead-letter status.

Every event handler must be idempotent.

## 5. Data ownership rules

- Catalog owns Product.
- Suppliers owns Supplier, SupplierProduct, SupplierOfferState and SupplierOfferObservation.
- Matching owns ProductBinding and match decisions.
- Monitoring owns MonitorTarget, MonitorAttempt, SourceHealth and MonitorDailyMetric.
- Pricing owns PriceCalculation and ProductPriceState.
- XML owns feed versions and offer projections.
- Orders owns order lifecycle.
- Purchasing owns procurement lifecycle.
- Inventory owns stock movements.
- Analytics owns derived projections and does not rewrite operational history.
- Audit owns outbox and audit records.

## 6. Critical workflow and transaction boundaries

### 6.1 Supplier check workflow

The workflow runs synchronously inside one worker task, but not inside one database transaction.

```text
A. Claim target transaction
   - claim due MonitorTarget
   - write lease token and lease_until
   - commit

B. External access, no database connection held
   - call Ozon/WB adapter
   - enforce timeout and cancellation

C. Observation transaction
   - persist MonitorAttempt outcome
   - update SupplierOfferState.last_checked_at
   - append SupplierOfferObservation when meaningful
   - update SourceHealth/target degradation
   - commit unconditionally when the source result is valid evidence

D. Pricing transaction
   - load the committed observation/state
   - validate ProductPricingPolicy
   - calculate price
   - optimistic-lock ProductPriceState
   - persist PriceCalculation
   - write secondary OutboxEvents
   - commit

E. Reschedule transaction
   - verify claim token
   - set next_check_at/backoff
   - release lease
   - commit
```

A pricing failure does not roll back or erase the observation. It records a pricing failure, leaves the previous valid price active and creates an actionable exception.

If the database is unavailable after the external source returned successfully, the result is not acknowledged as completed. The attempt is retried after the lease expires. Adapter requests should use stable request/observation fingerprints where possible so repeated persistence is idempotent.

### 6.2 XML workflow

XML generation reads an explicit pricing-state version. If newer pricing appears while generation is running, the generated version is either discarded before activation or published only when its input version still satisfies the activation rule.

## 7. Runtime architecture and concurrency

Initial runtime:

```text
One repository
One FastAPI API process
One Worker process
One PostgreSQL database
One Telegram adapter process or lightweight client
Web frontend later
```

The Worker is one process but not one sequential loop. It contains independent supervised asyncio loops:

```text
scheduler_loop
monitor_dispatch_loop
order_poll_dispatch_loop
outbox_dispatch_loop
maintenance_and_retention_loop
health_heartbeat_loop
```

Network-bound monitor tasks run with bounded concurrency using semaphores. A slow Ozon request may not block the outbox loop or order polling loop.

Rules:

- every external request has a hard timeout;
- blocking browser or parser work runs in a separate process/thread executor when required;
- each loop has its own exception boundary and restart policy;
- one failing loop must not terminate the whole Worker;
- graceful shutdown stops claims, waits for bounded in-flight work, and releases/lets leases expire safely;
- Worker heartbeat is stored separately from API health.

## 8. Scheduled task infrastructure

Supplier monitoring and Kaspi order polling reuse generic scheduling primitives:

- due timestamp;
- priority;
- lease owner/token;
- lease expiration;
- retry count;
- next retry time;
- execution diagnostics.

Business modules own their target tables and handlers. There is no universal god-table containing every business payload.

Bucketing/sharding fields are included for future scale, but the initial deployment may run one worker instance.

## 9. Database connection policy

Supabase Session Pooler is used for deployed processes.

Initial maximums:

```text
API:    pool_size=2, max_overflow=1
Worker: pool_size=2, max_overflow=1
```

Rules:

- do not hold a database connection during supplier HTTP/browser requests;
- use short transactions;
- monitor checked-out connections and pool timeout errors;
- Worker concurrency is bounded independently of database pool size;
- migrations run as a controlled deploy step, not concurrently from every process.

## 10. Authentication and authorization

Initial model:

- Telegram Bot -> API: `SERVICE_API_TOKEN` with restricted service scope;
- Web CRM -> API: Supabase Auth JWT;
- Owner/admin endpoints: owner JWT or explicitly permitted service scope;
- Worker -> application services: in-process calls, no internal HTTP;
- public XML endpoint: read-only and contains no admin capability.

Planned scopes include:

- products:read/write;
- bindings:review;
- monitoring:trigger;
- prices:override;
- purchases:review.

## 11. Failure modes and mitigations

### Database unavailable after a successful supplier response

- no database connection is held during scrape;
- persistence is retried using an observation fingerprint;
- the monitor target is not marked completed until persistence commits;
- lease expiry makes the task recoverable;
- repeated persistence failures move the target to degraded state and alert the owner.

### Pricing fails after observation is committed

- observation remains committed;
- previous valid ProductPriceState remains active;
- pricing failure is recorded separately;
- retry is scheduled after configuration/code correction;
- the product may be paused automatically when safe-price guarantees cannot be established.

### Worker dies while holding a target

- leases expire automatically;
- completion requires the original claim token;
- a stale worker may not reschedule or overwrite a newly claimed target.

### Captcha, rate limit or source block

- classify separately from product absence;
- apply source-level circuit breaker and increasing backoff;
- do not retry every product aggressively during a source-wide incident;
- surface manual-review status in Telegram;
- switch AccessStrategy only through configured policy.

### Outbox Dispatcher stops

- critical monitoring/pricing state remains committed;
- outbox backlog age and row count are health metrics;
- alerts trigger at configured warning/critical thresholds;
- dispatcher resumes with `SKIP LOCKED` and idempotent handlers;
- poison events move to dead-letter state after bounded attempts.

### XML generation fails

- active version is not changed;
- previous valid XML remains available;
- validation error and source pricing version are recorded;
- owner receives an exception notification.

### XML/object storage fills

- immutable versions have retention policy;
- active version and a recent rollback window are protected;
- maintenance loop deletes eligible old versions;
- storage usage has soft and hard thresholds;
- generation is blocked safely before storage exhaustion can corrupt publication.

### PostgreSQL connection limit reached

- fixed small pools;
- pool-timeout metrics and alerts;
- short transactions;
- bounded task concurrency;
- no DB connection during external network waits.

### Observation/history growth

- current state is separate from history;
- history is change-based, not one full snapshot per unchanged check;
- MonitorAttempt has short retention;
- daily rollups are produced before deletion;
- partitioning is introduced only when measured row volume/query latency crosses an agreed threshold.

## 12. Retention and maintenance

Initial policy, configurable per environment:

- MonitorAttempt: 14 days;
- bounded raw supplier payload: 3-7 days;
- SupplierOfferObservation: long-lived meaningful changes, with later archival policy;
- MonitorDailyMetric: indefinite or multi-year;
- successful XML versions: active + latest 20;
- failed XML file bodies: not retained indefinitely;
- Outbox processed rows: 7-30 days after processing;
- AuditLog and financial ledgers: long-lived.

Retention deletion is incremental and runs in bounded batches.

## 13. Reliability and operational health

Health is not one `/health` boolean. It includes:

- API availability;
- database connectivity;
- Worker heartbeat age;
- oldest due-but-unclaimed monitor target;
- monitor success rate;
- source health by platform;
- oldest unprocessed outbox event;
- outbox backlog size;
- XML active-version age;
- storage usage;
- connection-pool saturation.

Initial operational targets are internal SLOs, not customer promises:

- API admin operations: 99.5% monthly availability after paid always-on hosting;
- no active XML publication from an invalid file;
- critical notification backlog warning when oldest event exceeds 5 minutes;
- monitoring-lag warning when a high-priority target is overdue by more than two policy intervals;
- recovery from worker crash through lease expiry without manual database edits.

## 14. Decisions explicitly rejected for now

- microservices;
- Kafka or RabbitMQ in the first stage;
- direct Telegram access to PostgreSQL;
- pricing/scraping inside a user HTTP request;
- unlimited retries against a captcha or blocked source;
- one full historical snapshot for every unchanged check;
- automatic rebinding of confirmed products;
- destructive stock-history edits;
- automatic external checkout before procurement tracking is stable;
- unbounded XML and raw-response retention.

## 15. Development phases

### Phase A: foundation

- architecture and security contract;
- migrations and CI baseline;
- API authentication;
- Worker runtime skeleton;
- outbox and health foundation.

### Phase B: product and supplier core

- Product/Supplier/SupplierProduct/ProductBinding;
- lifecycle validation;
- manual CRUD;
- Telegram API client skeleton;
- import existing TGBAD bindings.

### Phase C: monitoring reliability vertical slice

Before broad monitoring, implement one real Ozon-bound product end to end:

- MonitorTarget and lease claiming;
- bounded async worker loops;
- Ozon adapter result classification;
- SupplierOfferState/Observation;
- MonitorAttempt and daily rollup;
- SourceHealth and degradation policy;
- transaction boundaries defined in section 6;
- operational metrics and Telegram exception output.

### Phase D: pricing and XML

- pricing policies and optimistic locking;
- manual override hierarchy;
- explainable PriceCalculation;
- versioned immutable XML;
- stable publication endpoint;
- storage retention.

### Phase E: web operations console

- dashboard;
- product and binding review;
- monitor/source health;
- price history and overrides;
- operational failures.

### Phase F: orders, purchasing and inventory

- Kaspi polling through shared scheduling primitives;
- purchase needs and status tracking;
- receipts and append-only stock movements;
- cancellations and profitability.

## 16. Phase C approval gate

Phase C coding may begin only when tests cover:

1. Two workers cannot complete the same lease claim.
2. A stale claim token cannot overwrite a newer claim.
3. Pricing failure does not roll back SupplierOfferObservation.
4. Outbox loop continues while a monitor request is slow.
5. Captcha is not classified as product absence.
6. Retention creates MonitorDailyMetric before deleting MonitorAttempt.
7. Database pool remains bounded during concurrent monitor requests.
8. XML active version never points to an invalid or incomplete file.

## 17. Immediate next step

Design and implement the ProductBinding and MonitorTarget state contracts for one controlled Ozon test product. Do not import the old parser wholesale. The first vertical slice must prove leases, transaction boundaries, source-error classification and diagnostics before scale is increased.
