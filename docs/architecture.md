# LEO CRM Architecture v0.1

Status: Draft
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

Telegram is not the system core. Telegram and the web interface are clients of the same API.

## 2. Core architectural principles

1. PostgreSQL is the single source of truth.
2. XML is generated output, never the primary storage.
3. Every Kaspi product is an independent business entity.
4. One product may have multiple supplier cards and multiple supplier platforms.
5. Monitoring, pricing, XML, orders and purchasing are separate modules.
6. No module writes directly into another module's internal tables without a service boundary.
7. All important changes are recorded as immutable business events and history records.
8. Background work is performed by jobs and workers, not by infinite loops inside the API process.
9. Telegram and the website communicate only through the API.
10. Secrets exist only in deployment environment variables.
11. Database structure changes only through Alembic migrations.
12. The system is optimized for speed, auditability and recovery after errors.

## 3. System context

```text
Kaspi XML consumer
        ^
        |
XML publication endpoint
        ^
        |
LEO CRM API <---- Web CRM
        ^
        |
Telegram assistant
        |
        v
PostgreSQL <---- Workers / Scheduler
        ^
        |
Supplier adapters: Ozon, WB, Kaspi, other connected sources
```

## 4. Main bounded modules

### 4.1 Catalog

Purpose: represent our own marketplace products.

Main entities:

- Product
- ProductStatus
- ProductAttribute
- ProductExternalIdentifier

Responsibilities:

- Kaspi product identity;
- merchant SKU;
- product name and brand;
- activation state;
- product-level settings;
- links to supplier bindings, pricing, orders and XML offers.

Catalog does not parse suppliers and does not calculate prices.

### 4.2 Suppliers

Purpose: represent external platforms and concrete supplier cards.

Main entities:

- Supplier
- SupplierAccount
- SupplierProduct
- SupplierOfferSnapshot

Supplier means a source/platform or configured supplier integration.

SupplierProduct means one concrete external card, for example one Ozon URL.

SupplierOfferSnapshot stores observed facts at a point in time:

- price;
- old price;
- availability;
- stock;
- delivery text;
- calculated delivery days;
- seller;
- observed timestamp;
- parser response metadata.

Historical snapshots are not overwritten.

### 4.3 Matching and bindings

Purpose: connect a Product with one or more SupplierProduct records.

Main entities:

- ProductBinding
- MatchCandidate
- MatchDecision

Binding stores:

- product_id;
- supplier_product_id;
- status;
- confidence score;
- source of decision: automatic/manual/imported;
- primary flag;
- priority;
- validation timestamps;
- last mismatch reason.

The matching engine only proposes candidates. It does not silently replace a confirmed binding without an explicit rule.

### 4.4 Monitoring

Purpose: independently schedule and execute checks for every bound supplier card.

Main entities:

- MonitorTarget
- MonitorPolicy
- MonitorJob
- MonitorRun
- MonitorError

Each binding has its own monitor state:

- last checked at;
- next check at;
- check interval;
- status;
- consecutive failures;
- last response time;
- lock owner and lock expiry.

Monitoring flow:

```text
Scheduler selects due targets
    -> creates jobs
    -> worker claims job
    -> supplier adapter checks card
    -> snapshot saved
    -> change detector compares state
    -> business event emitted
    -> next check scheduled
```

The scheduler must support many independent cards. We do not run one giant pass over the entire catalog.

### 4.5 Pricing

Purpose: calculate a safe sales price and repricing floor for each product.

Main entities:

- PricingPolicy
- ProductPricingPolicy
- PriceCalculation
- ProductPriceState

Inputs may include:

- supplier price;
- supplier delivery cost;
- marketplace commission;
- acquiring/payment cost;
- tax;
- fixed operating costs;
- desired profit;
- minimum margin;
- rounding rule;
- risk buffer;
- delivery SLA;
- product-specific floor.

The floor is dynamic. If supplier cost changes, the allowed minimum sale price changes.

Every calculation must store an explanation:

```text
supplier price
+ delivery
+ commission
+ tax
+ fixed costs
+ target profit
= safe sale price
```

Pricing never edits XML directly. It publishes a price decision.

### 4.6 XML

Purpose: generate and publish Kaspi-compatible XML from database state.

Main entities:

- XmlFeed
- XmlOfferState
- XmlGenerationRun
- XmlPublication

Flow:

```text
Approved product price/state
    -> XML projection updated
    -> XML generated
    -> validation performed
    -> version stored
    -> publication endpoint serves latest valid version
```

Requirements:

- atomic publication: Kaspi must never read a partially written file;
- previous valid version remains available if generation fails;
- generation history and validation errors are stored;
- each offer can be traced back to the price calculation that produced it.

### 4.7 Orders

Purpose: complete order lifecycle and business facts.

Main entities:

- Order
- OrderItem
- OrderStatusHistory
- Cancellation
- Fulfillment

Responsibilities:

- import orders from Kaspi;
- normalize statuses;
- preserve marketplace raw payload;
- track item quantities and revenue;
- detect cancellations;
- connect sales to purchasing and inventory;
- support profitability calculations.

### 4.8 Purchasing

Purpose: turn sales and stock needs into controlled procurement.

Main entities:

- PurchaseNeed
- PurchaseOrder
- PurchaseOrderItem
- PurchaseStatusHistory
- Receipt
- ReceiptItem

Lifecycle:

```text
Need detected
-> recommended supplier selected
-> purchase draft
-> purchased
-> in transit
-> received
-> accepted into inventory
-> linked to sold orders / stock
```

The first version will recommend and track purchases. Fully automatic checkout on external platforms is a later phase and must not be mixed into the initial architecture.

### 4.9 Inventory

Purpose: know what was purchased, received, reserved and sold.

Main entities:

- Warehouse
- StockItem
- StockMovement
- Reservation
- Batch

Inventory uses an append-only movement ledger. Current stock is calculated or projected from movements.

### 4.10 Finance and analytics

Purpose: calculate actual business performance.

Main entities:

- CostEntry
- RevenueEntry
- ProfitCalculation
- ProductDailyMetric
- SupplierMetric

Metrics include:

- revenue;
- gross profit;
- net profit;
- margin;
- cancellation rate;
- supplier price stability;
- monitoring reliability;
- purchase lead time;
- stock turnover;
- product profitability.

Financial calculations must be reproducible from source records.

### 4.11 Notifications and exception handling

Purpose: surface only actions that require human attention.

Channels:

- Telegram;
- web CRM;
- later email or other channels.

Examples:

- supplier card unavailable;
- price below safe floor;
- binding confidence degraded;
- XML generation failed;
- purchase overdue;
- order cannot be fulfilled;
- repeated parser errors.

Telegram is an assistant for exceptions, not the primary workflow engine.

### 4.12 Audit and events

Purpose: explain why the system changed something.

Main entities:

- BusinessEvent
- AuditLog
- OutboxEvent

Example events:

- SupplierOfferObserved
- SupplierPriceChanged
- SupplierAvailabilityChanged
- BindingConfirmed
- BindingRejected
- ProductPriceCalculated
- ProductPriceChanged
- XmlOfferChanged
- XmlPublished
- OrderImported
- OrderCancelled
- PurchaseNeedCreated
- PurchaseReceived

We will begin with a transactional outbox table inside PostgreSQL. We will not introduce Kafka or a complex message broker at the start.

## 5. Data ownership rules

- Catalog owns Product.
- Suppliers owns Supplier and SupplierProduct.
- Matching owns ProductBinding.
- Monitoring owns MonitorJob and MonitorRun.
- Pricing owns PriceCalculation and ProductPriceState.
- XML owns feed versions and offer projections.
- Orders owns order lifecycle.
- Purchasing owns procurement lifecycle.
- Inventory owns stock movements.
- Analytics reads source records and creates projections; it does not rewrite operational history.

## 6. Event flow example: supplier price changed

```text
1. Monitor worker checks an Ozon card.
2. SupplierOfferSnapshot is saved.
3. Change detector sees a price difference.
4. SupplierPriceChanged event is written to the outbox.
5. Pricing handler recalculates the safe sale price.
6. PriceCalculation and ProductPriceState are saved.
7. ProductPriceChanged event is written.
8. XML projection is updated.
9. New XML version is generated and validated.
10. Telegram receives a notification only if policy requires it.
```

Every step is idempotent. Reprocessing the same event must not create duplicate business actions.

## 7. Runtime architecture: initial stage

We deliberately start as a modular monolith, not microservices.

```text
One repository
One FastAPI application
One PostgreSQL database
One worker service
One scheduler service
One web frontend
One Telegram adapter
```

Why:

- faster development;
- simpler debugging;
- lower hosting cost;
- easier transactions;
- enough for the expected initial scale.

Modules remain separated in code so they can be extracted later if real load justifies it.

## 8. Deployment topology: initial stage

- GitHub: source control.
- Render Web Service: FastAPI API.
- Render Worker: background jobs.
- Render Cron or scheduler worker: due job creation.
- Supabase PostgreSQL: primary database.
- Supabase Storage or object storage later: XML versions and exported files.
- Web frontend deployment later.

Important limitation: Render free instances sleep. Continuous near-real-time monitoring cannot rely on the free web service. Before production monitoring, we must move workers to an always-on paid instance or another always-on host.

## 9. Repository target structure

```text
backend/
  app/
    api/
    core/
    db/
    modules/
      catalog/
      suppliers/
      matching/
      monitoring/
      pricing/
      xml_feed/
      orders/
      purchasing/
      inventory/
      analytics/
      notifications/
      audit/
    workers/
    main.py
frontend/
bot/
migrations/
docs/
tests/
render.yaml
```

## 10. Reliability requirements

- all jobs have retry policy and dead-letter state;
- external requests have timeouts;
- repeated failures use exponential backoff;
- workers use database locks or claim tokens;
- no duplicate processing for the same business key;
- latest valid XML remains available during failures;
- every automatic price change has a calculation trace;
- every manual change has actor, timestamp and reason;
- raw supplier responses may be retained for debugging with size limits;
- health checks distinguish API, database and worker health.

## 11. Security requirements

- no secrets in GitHub;
- private CRM authentication before real business data is loaded;
- role model initially: owner and system;
- supplier credentials encrypted at rest where applicable;
- public XML endpoint is read-only and exposes only required feed data;
- admin API is never public without authentication;
- audit log cannot be modified through normal application endpoints.

## 12. Decisions we explicitly reject for now

- microservices;
- Kafka or RabbitMQ at the first stage;
- React frontend before the operational core exists;
- automatic purchasing on external platforms before purchase tracking is stable;
- one giant monitoring loop;
- storing current business state only in XML or JSON files;
- direct Telegram access to the database;
- hardcoded supplier/product mappings in the matching engine;
- uncontrolled automatic rebinding of confirmed products.

## 13. Development phases

### Phase A: foundation

- architecture document;
- authentication plan;
- modular project structure;
- database migrations;
- tests and CI baseline;
- event/outbox foundation.

### Phase B: product and supplier core

- products;
- suppliers;
- supplier products;
- bindings;
- manual CRUD and validation;
- import existing TGBAD bindings.

### Phase C: monitoring engine

- monitor policies;
- due jobs;
- worker claiming;
- Ozon adapter;
- snapshots and history;
- retry and error states.

### Phase D: pricing and XML

- pricing policies;
- product-level floors;
- price calculations;
- XML projection;
- validation and publication endpoint.

### Phase E: web operations console

- dashboard;
- product card;
- binding review;
- monitor status;
- price history;
- system errors.

### Phase F: orders, purchasing and inventory

- Kaspi order import;
- purchase needs;
- procurement statuses;
- receipts;
- stock movements;
- cancellation and profitability analytics.

### Phase G: automation expansion

- automatic supplier selection;
- intelligent exception handling;
- purchasing recommendations;
- selective automatic purchase actions;
- forecasting and optimization.

## 14. Next architecture decisions to finalize

Before further production code, we must approve:

1. Product lifecycle and statuses.
2. Binding lifecycle and automatic/manual decision rules.
3. Monitoring frequency, priority and failure policy.
4. Exact pricing formula and product-level overrides.
5. XML publication contract for Kaspi.
6. Order and purchase lifecycle.
7. Authentication and access model.
8. Event naming and idempotency rules.
9. Data retention for snapshots and logs.
10. Production hosting requirements for continuous monitoring.

## 15. Immediate next step

The next work item is not another parser. We will design the lifecycle of a Product and ProductBinding, because these two state machines determine how monitoring, pricing and human review behave.
