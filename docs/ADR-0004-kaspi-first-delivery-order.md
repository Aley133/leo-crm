# ADR-0004: Kaspi-first delivery order

Status: Accepted
Date: 2026-07-19
Decision owners: Product owner, technical lead, CTO reviewer

## Context

LEO is evolving from a supplier-monitoring bot into a Kaspi-first marketplace operations platform. The commercial workflow begins with a Kaspi order and continues through purchasing, supplier selection, margin calculation, inventory, finance and analytics.

The previous roadmap placed Browser Runtime immediately after Monitoring Stabilization. A later proposal moved Marketplace Core, Purchase Engine and Pricing Engine before Browser Runtime. CTO review accepted the Kaspi-first direction but rejected placing Pricing and supplier recommendation before a working supplier access strategy.

The rejected ordering would have recreated a previously identified risk: pricing and recommendation would be validated mainly with synthetic supplier data. Direct HTTP access to Ozon is already known to be unreliable for the required flow, including observed HTTP 403 responses.

## Decision

The delivery order is:

```text
Phase C — Monitoring Stabilization
    ↓
Phase D — Marketplace Core (Kaspi-first)
    ↓
Phase E — Purchase Lifecycle Core
    ↓
Phase F — Browser Runtime MVP (Ozon live price feed)
    ↓
Phase G — Pricing Engine
    ↓
Phase H — Supplier Recommendation
```

Later phases cover XML publication, production worker activation, Telegram operations, Web CRM, inventory expansion and analytics.

## Boundaries

### Marketplace Core

Marketplace Core owns normalized marketplace concepts and workflows. Kaspi is the first mandatory integration, but the core model must not mirror Kaspi API payloads.

Core concepts use marketplace-neutral names such as:

- `MarketplaceAccount`;
- `MarketplaceOrder`;
- `MarketplaceOrderLine`;
- `MarketplaceOrderEvent`;
- `ExternalMarketplaceReference`.

Kaspi-specific identifiers, statuses and raw payloads belong to the Kaspi integration boundary and are translated into core values.

### Purchase Lifecycle Core

Purchase Lifecycle may be implemented before Browser Runtime only for deterministic lifecycle behavior:

- create purchase request;
- transition status;
- record receipts and cancellations;
- preserve audit history;
- link a purchase request to an originating marketplace order.

It must not select or recommend a supplier in this phase.

### Browser Runtime MVP

Browser Runtime is infrastructure. It does not own:

- marketplace orders;
- monitoring transactions;
- scheduling policy;
- source-health policy;
- pricing policy;
- purchase decisions;
- XML publication.

The MVP is intentionally narrow: one production-shaped Ozon access strategy sufficient to obtain normalized live supplier price, availability and delivery evidence through the existing adapter contract.

### Pricing Engine

Pricing begins only after the Ozon Browser Runtime MVP provides a real observation stream. Synthetic fixtures remain useful for deterministic edge cases, but acceptance requires real-adapter contract tests and captured representative evidence.

### Supplier Recommendation

Recommendation is separated from Purchase Lifecycle. It consumes accepted supplier observations and explainable pricing results. It must not be embedded in adapters or Browser Runtime.

## Transaction ownership

Application orchestrators own transactions. Integration adapters and Browser Runtime never commit business state directly.

Kaspi import follows this sequence:

```text
fetch external data without an open database transaction
    ↓
normalize and validate
    ↓
application transaction persists raw import identity, normalized order and import checkpoint atomically
```

## Consequences

Positive:

- Kaspi business value can be delivered without waiting for browser automation;
- order and purchase lifecycle models remain reusable for future marketplaces;
- pricing and recommendation are validated against live supplier data;
- Browser Runtime remains replaceable infrastructure;
- lifecycle and recommendation responsibilities are not mixed.

Costs:

- the roadmap contains a deliberately small Browser Runtime MVP before the full runtime;
- Kaspi payload translation and raw-payload retention require explicit contracts;
- existing Kaspi-specific fields in generic models must be reviewed and migrated incrementally rather than copied into new core entities.

## Non-decisions

This ADR does not yet approve:

- the final Kaspi API polling cadence;
- automatic purchasing;
- supplier recommendation policy;
- pricing formulas;
- browser pool, profile cluster or anti-bot implementation beyond the MVP;
- destructive migration of existing product data.

Each requires its own contract and acceptance tests.

## Acceptance

This ADR is implemented when:

- the roadmap reflects the approved phase order;
- a Kaspi integration contract defines raw-to-normalized boundaries and idempotency;
- Marketplace Core begins with tests that do not import Kaspi transport schemas into domain entities;
- Pricing and recommendation remain blocked until a live Ozon Browser Runtime MVP is proven.
