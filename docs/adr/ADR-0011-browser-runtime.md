# ADR-0011: Browser Runtime as replaceable infrastructure

## Status

Accepted.

## Context

Supplier monitoring already supports multiple access strategies through the `SupplierAdapter` protocol and `AccessStrategy`. Direct HTTP is insufficient for supplier pages that require JavaScript, authenticated profiles or browser-visible state. Embedding Playwright/Selenium calls directly inside each supplier adapter would duplicate lifecycle, timeout, cleanup and telemetry logic and would make testing and future runtime replacement expensive.

The project also has two independent product binding surfaces:

- demand identity through `MarketplaceListing -> Product`;
- supply identity through `ProductBinding -> SupplierProduct -> Product`.

Browser access belongs to the supply-side observation path. It must not be coupled to marketplace listings, orders or purchase creation.

## Decision

Introduce a framework-neutral `BrowserRuntime` protocol between supplier adapters and the concrete browser technology.

```text
Monitoring Engine
      |
      v
SupplierAdapter (AccessStrategy.BROWSER)
      |
      v
BrowserRuntime
      |
      v
Playwright / remote browser / future implementation
```

The runtime owns transport lifecycle and returns browser evidence. Supplier adapters own source-specific navigation and normalization into `NormalizedOffer`. Monitoring continues to own retries, breaker state, scheduling and persistence.

The initial runtime contract is asynchronous and uses immutable request/response value objects. Runtime errors use an explicit classification enum. Concrete Playwright wiring is an implementation detail and may be replaced without changing supplier adapter business contracts.

No database transaction may remain open during browser execution.

## Consequences

Positive:

- one implementation of timeout, cleanup and session isolation;
- source adapters remain testable with a fake runtime;
- Playwright is not leaked into domain/application contracts;
- direct HTTP and browser access keep independent `SourceHealth` through existing `(supplier_id, access_strategy)` scoping;
- future remote-browser infrastructure can replace the in-process implementation.

Trade-offs:

- one extra abstraction between an adapter and browser technology;
- runtime and adapter failure classifications must be mapped carefully;
- authenticated profile concurrency needs explicit limits.

## Explicitly deferred

- distributed browser workers;
- database-backed browser session leases;
- proxy pools;
- CAPTCHA solving;
- automatic supplier checkout.
