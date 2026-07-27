# LEO CRM baseline before workspace sessions

Source of truth: owner backup `leo-crm-main.zip`, saved before login/password and workspace UI migration.

The workspace migration must preserve these interfaces and actions. Only authentication and data ownership may change.

## Dashboard

- Overview metrics
- Attention states
- Runtime and queue status
- Supplier and offer counters

## Product Center

- XML import, preview and commit
- Search and status filter
- Only without supplier
- Only monitoring failures
- Only monitored
- Sales, revenue, supplier, price and monitoring columns
- Navigation to the full product card

## Product card

- General Kaspi product data
- Sales and revenue
- Realized and per-sale economics
- FIFO inventory batches: create, edit and delete
- Best supplier offer
- CRM action recommendation
- Decision timeline
- Procurement sources: online, offline and production
- Supplier observations

## Orders Center

- Kaspi order import/rebuild
- Revenue and margin snapshot
- Search and stage filters
- Full order actions and procurement state

## Revenue

- Daily revenue and margin history
- Manual snapshot capture

## Suppliers

- Search and supplier-code filter
- Availability filter
- Only unbound
- Only failures

## Monitoring

- Browser Agent download and presence
- Queue, leases, attempts and source state
- Job details and error filtering

## Dumping and XML

- Competitor agent status
- Public XML source
- Product policy selection
- Safe floor policy
- Auto-publish controls

## Migration invariant

BARWORK and every new store use the same HTML/CSS/business behavior. A workspace session replaces `SERVICE_API_TOKEN`, and every read/write is restricted by `workspace_id`. No reduced parallel CRM is allowed.
