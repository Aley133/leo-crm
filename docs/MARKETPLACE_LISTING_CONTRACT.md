# Marketplace Listing Identity Contract

## Status

Approved implementation contract for Product Identity Sprint 1.

## Purpose

`MarketplaceListing` is the boundary between marketplace-specific identifiers and the canonical internal `Product`.

A marketplace order line does not identify a `Product` directly. It carries external identifiers observed from the marketplace. Those identifiers resolve through a marketplace listing owned by one marketplace account.

```text
MarketplaceOrderLine
        -> MarketplaceListing
        -> Product (nullable until confirmed)
```

## Identity precedence

For one marketplace order line, identity is selected in this order:

1. non-blank `merchant_sku`;
2. otherwise non-blank `external_product_id`.

The stored `identity_key` is namespaced by identity kind:

```text
merchant_sku:<value>
external_product_id:<value>
```

The database uniqueness boundary is:

```text
(marketplace_account_id, identity_key)
```

This prevents accidental collisions between a merchant SKU and an external product ID containing the same raw string.

## Missing identity

If both `merchant_sku` and `external_product_id` are absent or blank:

- no `MarketplaceListing` is created;
- the order line remains unresolved;
- one `MarketplaceListingIssue` is upserted for the order line;
- its reason is `missing_identity`;
- separate lines must never be merged under an empty or synthetic listing key.

When a later import supplies a usable identity, the open issue may be marked resolved, but the original issue record remains as historical evidence.

## Automatic listing creation

Importing an identified marketplace order line automatically ensures an unresolved `MarketplaceListing` exists.

First creation must be concurrency-safe. The implementation uses:

```sql
INSERT ... ON CONFLICT (marketplace_account_id, identity_key) DO NOTHING
```

followed by a re-select in the same caller-owned transaction.

The implementation must not use an unprotected `SELECT` followed by `INSERT`, and must not commit internally.

## Resolution

A newly discovered listing has:

```text
product_id = NULL
status = unresolved
```

A later explicit command may bind it to a canonical `Product` and set status to `resolved`.

Sprint 1 does not guess a product from title similarity and does not automatically create canonical products.

## Historical purchase semantics

Binding, unbinding, or rebinding a `MarketplaceListing` affects future resolution only.

It must never mutate an already-created `PurchaseRequestLine.product_id`. A purchase line preserves the product identity captured at the moment the purchase line was created. This is an append-only historical fact, equivalent to preserving prior supplier observations.

## Transaction boundary

Marketplace import owns the transaction. In the same transaction it persists:

```text
raw marketplace evidence
+ normalized order and lines
+ ensured marketplace listings or missing-identity issues
+ status events
+ checkpoint
```

Product identity helpers never commit or roll back the caller's transaction.

## Explicit exclusions

This sprint does not implement:

- automatic product creation;
- title-based fuzzy matching;
- supplier selection;
- inventory reservation;
- procurement-need calculation;
- mutation of historical purchase lines after listing rebinding.
