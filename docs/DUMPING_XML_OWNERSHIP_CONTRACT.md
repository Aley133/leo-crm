# Dumping XML ownership contract

Status: production business rule

## Source baseline

The last XML confirmed through the product-registry import is the complete
baseline for that workspace. Import resets the generated feed to that exact
business state before any managed overlays are applied. A generated value from
an older XML upload must not leak into the newly imported baseline.

## Explicit ownership

LEO CRM may change an offer in the generated Kaspi XML only when the matching
product has one `DumpingPolicy` with both flags enabled:

- `enabled = true`;
- `auto_publish_xml = true`.

Product-registry membership, historical policies, FIFO batches, active orders
and unresolved order identities do not grant XML ownership.

For an unmanaged offer, CRM preserves the imported price, `available`,
`preOrder`, `stockCount` and all other source fields. Inventory allocation,
order reconciliation, supplier monitoring and dumping background jobs must not
change or remove that offer.

## Managed overlay

After a new XML import, only managed offers are projected from current CRM
state. Their price, FIFO stock or supplier preorder state and delivery period
continue through the existing dumping workflow. Unmanaged offers remain equal
to the new source baseline.

An unresolved active order may close an offer only when its identity resolves
uniquely to a managed product. CRM never guesses between multiple products and
never closes an arbitrary source offer.

## Import runtime contract

Preview is read-only. It must not retain, activate or otherwise write an XML
source in the background. The source becomes authoritative only after the user
confirms the import and the database transaction commits successfully.

XML parsing, catalog persistence, order-line linking and managed overlays run
outside the asynchronous web loop. The database session is created and closed
inside that bounded worker. Order-line linking reads only identities present in
the imported catalog; it must not materialize unrelated historical order lines.
Concurrent XML writers serialize on the active workspace feed row.

## Required regression checks

- reimport replaces a previously generated/zeroed baseline;
- enabled auto-publishing offers receive the managed overlay;
- offers without a policy remain unchanged even when FIFO or orders exist;
- disabled policies remain unchanged;
- enabled policies with XML auto-publication disabled may calculate but do not
  publish;
- workspace isolation remains mandatory for source and generated feeds.
