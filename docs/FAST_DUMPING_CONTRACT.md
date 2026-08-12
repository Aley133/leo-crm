# Fast Dumping runtime contract

Fast Dumping is an isolated realtime pricing channel. It does not reuse or
mutate `DumpingPolicy`, `DumpingRun`, `KaspiXmlFeed`, the ordinary dumping
scheduler, or the XML projection.

## Ownership

- Every policy, state, job, claim, heartbeat, and API query is scoped by
  `workspace_id`.
- Before a heartbeat or claim is accepted, CRM verifies that the agent's
  Merchant UID equals the Kaspi Partner ID stored for that workspace.
- The local Fast Agent keeps a separate configuration per workspace.
- Merchant Cabinet credentials and `mc-sid` remain on the Windows machine and
  are protected with Windows DPAPI. They are never sent to CRM or written to
  job/result JSON.

## Decision and apply sequence

1. The local agent scans the public product card and Offers API.
2. Buyer-context guards reject API prices below the public headline price.
3. CRM reads the current FIFO source and calculates the safe floor with the
   existing commission, tax, logistics, and minimum-profit formula.
4. CRM persists a versioned decision before a write can be claimed.
5. Immediately before writing, CRM recalculates the source, floor, target, and
   physical stock. A changed value makes the job stale and queues a fresh scan.
6. The local agent writes the target price together with the freshly confirmed
   physical stock.
7. HTTP 200 is `PENDING`. The operation is `APPLIED` only after the scanner sees
   the target price in our own merchant row.

## Safety invariants

- Realtime writes are allowed only when physical FIFO stock is greater than
  zero and the selected cost source is `inventory`.
- Supplier/preorder products remain outside Fast Dumping. Their XML behavior is
  unchanged.
- One product has at most one active Fast Dumping job.
- A lost apply lease becomes a verification-only job; it is not written again.
- An accepted but unverified operation latches an automatic-write pause for
  that product. Only the explicit Resume action clears it.
- A product at `floor_limited` remains monitored and appears in the dedicated
  threshold queue. Lowering the threshold means explicitly changing minimum
  profit; the UI never bypasses CRM's floor calculation with an arbitrary
  target price.
- Market-context mismatch, missing own offer, price anomaly, missing inventory,
  or disabled sale state blocks realtime writes without changing XML.
