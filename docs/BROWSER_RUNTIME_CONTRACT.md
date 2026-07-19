# Browser Runtime Contract

## Status

Approved implementation contract for Sprint 3 — Browser Runtime MVP.

## Purpose

Browser Runtime is infrastructure for executing bounded browser interactions on behalf of supplier adapters. It owns browser-session lifecycle, request isolation, timeouts, cleanup and transport-level diagnostics. It does not understand supplier products, marketplace orders, pricing, purchasing or inventory.

## Domain boundary

`Product` has two independent binding surfaces:

```text
Demand side: MarketplaceListing -> Product
Supply side: ProductBinding -> SupplierProduct -> Product
```

`MarketplaceListing` identifies what was sold. `ProductBinding` identifies where the same internal product may be sourced. These surfaces must not be merged for convenience.

## Responsibility split

### BrowserRuntime

Owns:

- acquiring and releasing a browser session;
- enforcing one bounded execution deadline;
- opening and closing isolated pages/contexts;
- returning transport evidence;
- classifying runtime failures;
- guaranteed cleanup;
- runtime telemetry.

Does not own:

- Ozon/Wildberries/Kaspi selectors;
- offer normalization;
- supplier matching;
- retries or circuit-breaker policy;
- database transactions;
- business persistence.

### BrowserSupplierAdapter

Owns supplier-specific navigation, selectors and normalization into the existing `NormalizedOffer` contract. It implements the existing `SupplierAdapter` protocol with `AccessStrategy.BROWSER`.

### Monitoring Engine

Owns scheduling, retry, backoff, `SourceHealth`, breaker decisions and persistence of attempts/observations. No database transaction remains open while Browser Runtime performs external work.

## Request contract

A runtime request contains only transport concerns:

- absolute URL;
- operation name for telemetry;
- timeout;
- optional session/profile key;
- optional wait condition;
- bounded metadata safe for logs.

The runtime must reject empty or non-HTTP(S) URLs and non-positive timeouts.

## Response contract

A successful response contains:

- final URL;
- page content snapshot required by the adapter;
- observed timestamp;
- duration;
- runtime/session identifiers safe for diagnostics;
- bounded metadata.

Business facts such as price, availability and delivery days are never fields of a runtime response.

## Failure taxonomy

Runtime failures are explicit and machine-readable:

```text
timeout
navigation_failed
browser_unavailable
session_expired
captcha
blocked
auth_required
invalid_response
unexpected
```

Supplier adapters may enrich this classification, but must not collapse all failures into one generic exception.

## Lifecycle and concurrency

1. A runtime execution has one owner and one deadline.
2. Session acquisition must be concurrency-safe.
3. Page/context resources are released in `finally`, including timeout and cancellation paths.
4. A poisoned session is discarded rather than returned to the reusable pool.
5. Runtime implementations may reuse authenticated profiles, but concurrent executions must not mutate the same profile unsafely.
6. MVP provides an in-process abstraction; durable distributed session coordination is deferred until measured concurrency requires it.

## Transaction boundary

The required flow is:

```text
claim monitoring target in short transaction
-> close transaction
-> execute Browser Runtime
-> open persistence transaction
-> persist attempt, observation, state and reschedule
```

No SQLAlchemy session or business transaction may be held open during browser navigation.

## Security

- credentials, cookies and profile paths are supplied through deployment secrets/configuration;
- secrets are never included in response metadata or logs;
- HTML snapshots are bounded and may require redaction;
- arbitrary script execution supplied by API callers is forbidden;
- Browser Runtime is internal worker infrastructure and is not exposed as a generic public browsing API.

## MVP scope

Sprint 3 MVP includes:

- framework-neutral runtime protocol and value objects;
- explicit failure taxonomy;
- deterministic timeout/cleanup behavior;
- a fake runtime for adapter tests;
- first Ozon browser adapter integration against the existing `SupplierAdapter` contract.

The MVP does not yet include:

- a distributed browser farm;
- proxy rotation;
- automatic CAPTCHA solving;
- automatic checkout;
- persistent browser-session database tables;
- arbitrary remote browser control from Telegram or Web CRM.
