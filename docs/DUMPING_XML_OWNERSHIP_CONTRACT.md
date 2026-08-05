# Dumping XML ownership contract

Status: production business rule

## Cumulative source baseline

Every XML confirmed through the product-registry import is an upsert into one
persistent workspace catalog. Offers present in the new file replace the same
SKU from the previous baseline; offers absent from the new file remain in the
baseline and generated feed. A partial export must never delete earlier
products or offers.

After the merge, the generated feed is reset to the cumulative source baseline
before managed overlays are applied. A generated price or availability value
from an older publication must not leak into that baseline.

## Explicit ownership

LEO CRM may change an offer in the generated Kaspi XML only when the matching
product has one `DumpingPolicy` with both flags enabled:

- `enabled = true`;
- `auto_publish_xml = true`.

Product-registry membership, historical policies, FIFO batches, active orders
and unresolved order identities do not grant XML ownership.

The one exception is the explicit product sale switch. `sale_enabled = false`
is a durable owner command to close that offer with `available=no`,
`preOrder=0`, and `stockCount=0`. It does not delete FIFO, supplier bindings or
the dumping policy. While the switch is off, periodic/manual competitor jobs
must not be queued and stale results must not reopen the offer. Turning it on
removes the latch and returns the offer to the ordinary inventory/dumping flow.

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
- partial reimport preserves offers absent from the uploaded file;
- newly imported `available=no` products remain outside dumping until a policy
  is explicitly connected;
- the manual out-of-stock switch survives reimport and blocks publication;
- enabled auto-publishing offers receive the managed overlay;
- offers without a policy remain unchanged even when FIFO or orders exist;
- disabled policies remain unchanged;
- enabled policies with XML auto-publication disabled may calculate but do not
  publish;
- workspace isolation remains mandatory for source and generated feeds.
