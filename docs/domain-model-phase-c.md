# LEO CRM Domain Model — Phase C Vertical Slice

Status: Draft for implementation review
Scope: Suppliers, Matching, Monitoring, Pricing and XML publication only

This document does not redefine transaction boundaries from `docs/architecture.md`. Module ownership and transaction boundaries remain authoritative. The term aggregate is used strictly as a transactional consistency boundary.

## 1. Bounded contexts

### Marketplace Catalog Context

Canonical term `Product` means one sellable Kaspi marketplace card managed by LEO CRM.

It is not a physical warehouse item and not a supplier listing.

Owned entities:

- Product
- ProductStatus
- ProductExternalIdentifier

### Supplier Context

Canonical term `SupplierProduct` means one concrete external supplier listing, such as one Ozon or WB card.

Owned entities:

- Supplier
- SupplierProduct
- SupplierOfferState
- SupplierOfferObservation
- SourceHealth

### Matching Context

Canonical term `ProductBinding` means an explicit relationship between one Product and one SupplierProduct.

Owned entities:

- ProductBinding
- MatchCandidate
- MatchDecision

### Monitoring Context

Canonical term `MonitorTarget` means the schedule and lease state for checking one confirmed binding.

Owned entities:

- MonitorTarget
- MonitorAttempt
- MonitorDailyMetric

### Pricing Context

Canonical term `PriceCalculation` means one immutable and explainable pricing decision.

Owned entities:

- PricingPolicy
- ProductPricingPolicy
- PriceCalculation
- ProductPriceState

### XML Publication Context

Canonical term `XmlFeedVersion` means one immutable generated and validated XML artifact.

Owned entities:

- XmlFeed
- XmlOfferState
- XmlFeedVersion
- XmlGenerationRun

## 2. Aggregate roots and consistency boundaries

### Product

Aggregate root of the Marketplace Catalog Context only.

Product does not transactionally own ProductBinding, ProductPriceState or XmlOfferState. Those records are owned by their respective modules and are connected by identifiers and public application services.

### SupplierProduct

Aggregate root of supplier listing identity.

`SupplierOfferState` is a separate current-state projection with one row per SupplierProduct. `SupplierOfferObservation` is append-only history and is committed independently from pricing.

### ProductBinding

Aggregate root of matching decisions.

Lifecycle:

```text
candidate -> confirmed -> active -> degraded -> disabled
candidate -> rejected
confirmed -> rejected only by explicit manual decision
```

A confirmed binding may not be silently replaced by an automatic matcher.

### MonitorTarget

Aggregate root of scheduling and lease ownership.

A target may be changed only by the current valid lease token or by an explicit administrative recovery operation.

### ProductPriceState

Aggregate root of current effective price state in the Pricing Context.

It uses optimistic locking and may be updated only if the expected version matches the stored version.

### XmlFeed

Aggregate root of publication state.

It points to exactly one active immutable XmlFeedVersion. Generating a version and activating a version are separate operations.

## 3. Value objects

### Money

Fields:

- amount: Decimal
- currency: ISO-like code, initially `KZT` and `RUB`

Rules:

- never use float arithmetic;
- money with different currencies cannot be added without an explicit conversion operation;
- amount precision is fixed by business rules.

### Fingerprint

A deterministic SHA-256 value built from normalized supplier facts:

- supplier_product_id;
- price;
- availability;
- stock;
- delivery_days;
- seller;
- adapter_schema_version.

All automatic access strategies must produce a stable Fingerprint.

### LeaseToken

A cryptographically random opaque token identifying one claim generation.

Equality is exact. A stale token may not reschedule, release or overwrite a newer claim.

### ConfidenceScore

Integer from 0 to 100.

It expresses matching confidence only and does not activate a binding by itself.

### DeliveryDays

Non-negative integer with an explicit unknown state represented by `None`, not by zero.

### PriceStateVersion

Monotonic integer used by optimistic locking and XML stale-input detection.

## 4. Canonical domain events

This is the canonical Phase C event list. Event names in code and outbox must match these names exactly.

- `BindingConfirmed`
- `BindingRejected`
- `SupplierOfferObserved`
- `SupplierOfferChanged`
- `SupplierPriceChanged`
- `SupplierAvailabilityChanged`
- `SourceHealthChanged`
- `ProductPriceCalculated`
- `ProductPriceChanged`
- `ProductPriceOverrideApplied`
- `XmlGenerationRequested`
- `XmlGenerationRejectedAsStale`
- `XmlPublished`

No second synonym may be introduced for the same business fact without an architecture change.

## 5. Policies

### MonitoringPolicy

Defines:

- check interval;
- timeout;
- retry count;
- backoff;
- lease duration;
- degradation thresholds;
- circuit-breaker behaviour.

Lease duration must cover adapter timeout, retries, observation write budget, pricing budget and safety margin.

### MatchingPolicy

Defines candidate thresholds and rules for manual confirmation. It may propose but may not silently replace a confirmed binding.

### PricingPolicy

Defines the explainable price chain and manual override priority.

Priority order:

```text
sale prohibition
> manual lock
> manual fixed price
> automatic calculation
```

### PublicationPolicy

Defines whether a ProductPriceState is eligible for XML publication and how stale generated versions are rejected.

## 6. Application services

- `BindingService`
- `MonitoringService`
- `ObservationService`
- `PricingService`
- `XmlPublicationService`

A module may call another module only through its public application service or a canonical event. A module may not directly write another module's internal repository.

## 7. Mandatory invariants

1. SupplierOfferObservation is immutable and append-only.
2. PriceCalculation is immutable and idempotent for the same observation and pricing-policy version.
3. SupplierOfferState has at most one current row per SupplierProduct.
4. A stale LeaseToken cannot reschedule, release or overwrite a newer claim.
5. External HTTP work never holds a database connection.
6. A committed observation is never rolled back because pricing failed.
7. A pricing failure keeps the previous valid ProductPriceState active and creates a visible exception.
8. Captcha, rate limiting, authentication failure and source blocking are not product absence.
9. A confirmed binding is not silently replaced by automation.
10. Manual pricing decisions have priority over automatic recalculation.
11. ProductPriceState updates require optimistic version matching.
12. XML generation reads explicit ProductPriceState versions.
13. Before activation, every referenced ProductPriceState version is rechecked.
14. A stale XML version is never activated and a new generation is scheduled.
15. The public XML endpoint serves only an immutable, existing and validated active version.
16. MonitorAttempt is rolled up into MonitorDailyMetric before raw attempt retention deletes it.
17. Fingerprint generation is mandatory for every automatic adapter strategy.
18. Network concurrency and database-write concurrency use separate bounded semaphores.

## 8. Transaction boundaries

The supplier-check workflow remains:

```text
A. claim target in a short transaction
B. call external source without a DB connection
C. commit attempt and observation/state in a short transaction
D. calculate and commit pricing in a separate short transaction
E. reschedule and release lease using the same valid claim token
```

These steps are intentionally not one aggregate transaction.

## 9. Out of scope

Orders, Purchasing, Inventory and Analytics receive their own domain models before their implementation phases. This document must not invent their aggregates or events in advance.
