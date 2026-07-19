# Purchase Lifecycle Core Contract

## Status

Approved implementation contract for Phase E — Purchase Lifecycle Core.

## Purpose

The purchase domain records the internal lifecycle of acquiring goods required by a marketplace order or by manual demand. It does not select a supplier, calculate a recommended price, call a marketplace API, or perform browser automation.

## Boundary

Marketplace Core owns imported sales orders. Purchase Lifecycle owns internal purchase requests, their lines, receipts and audit events.

A purchase request may reference a `MarketplaceOrder`, but it must never depend on the raw Kaspi payload. Manual purchase demand is also valid and therefore `marketplace_order_id` is nullable.

Supplier recommendation is explicitly outside this phase. A supplier reference may be added later only after the recommendation contract is approved.

## Aggregate

`PurchaseRequest` is the aggregate root.

Owned records:

- `PurchaseRequestLine`;
- `PurchaseEvent`;
- `PurchaseReceipt`;
- `PurchaseReceiptLine`.

## Lifecycle

Canonical statuses:

```text
draft
requested
ordered
partially_received
received
cancelled
closed
```

Initial transition policy:

```text
draft -> requested | cancelled
requested -> ordered | cancelled
ordered -> partially_received | received | cancelled
partially_received -> partially_received | received | cancelled
received -> closed
cancelled -> closed
closed -> terminal
```

Transitions are application-service decisions. Persistence helpers do not commit and do not bypass transition validation.

## Invariants

1. Quantity is always greater than zero.
2. Received quantity is never negative and never greater than requested quantity.
3. A receipt quantity is always greater than zero.
4. Every status transition creates an append-only `PurchaseEvent` in the same transaction.
5. Every accepted aggregate mutation increments `PurchaseRequest.version`.
6. Event idempotency is scoped by `(purchase_request_id, idempotency_key)`.
7. Receipt-line identity is scoped by `(purchase_receipt_id, purchase_request_line_id)`.
8. Deleting an originating marketplace order is restricted while a purchase request references it.
9. The application layer owns commit/rollback.

## Transaction boundary

For a lifecycle command, the application service atomically persists:

```text
aggregate state
+ changed lines or receipts
+ append-only audit event
+ optional transactional outbox event
```

No external HTTP or browser request may execute while this business transaction is open.

## Explicit exclusions

Phase E does not implement:

- automatic supplier selection;
- purchasing on Ozon or Wildberries;
- pricing calculations;
- warehouse FIFO movements;
- payment accounting;
- automatic order creation at a supplier;
- dependence on Kaspi transport DTOs or raw payloads.
