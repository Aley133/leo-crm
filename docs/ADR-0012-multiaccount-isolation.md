# ADR-0012: Multi-account isolation without UI duplication

Status: prepared for release, not deployed

## Decision

LEO CRM keeps one set of existing pages and selects an active workspace through
the `X-Workspace-ID` request header. The shared `workspace-context.js` module
adds that header to same-origin private API calls, manages Kaspi connections in
an existing-page dialog, and reloads the page after switching accounts so no
cached rows survive the boundary.

Every operational aggregate is workspace-owned: orders and raw payloads,
products, suppliers and monitoring, FIFO inventory, purchases, pricing and
dumping, XML feeds, revenue snapshots, outbox events and durable agent jobs.
SQLAlchemy adds the workspace predicate to reads and bulk mutations and rejects
ORM writes whose owner differs from the current request context.

Kaspi API tokens are encrypted at rest with `KASPI_CREDENTIALS_KEY`. Tokens are
accepted on create/update and are never returned by the API. The legacy Render
credentials are copied into workspace 1 once; stored CRM credentials win on all
later starts.

Browser Agent and Kaspi Competitor Agent remain shared executors. Their durable
jobs carry `workspace_id`; claim/complete endpoints may inspect all workspaces,
but business results are applied inside the job owner's context. Automatic
Kaspi polling iterates active credentials and passes each account's token and
marketplace account ID explicitly.

Workspace 1 retains `/feeds/kaspi/catalog.xml`. Every workspace also has a
stable independent URL `/feeds/kaspi/{workspace_slug}/catalog.xml`.

## Migration and rollback

Migration `20260731_0028` assigns all pre-existing operational data to
workspace 1 and adds ownership indexes and foreign keys. Supplier codes become
unique per workspace, while Kaspi Partner IDs remain globally unique to prevent
the same shop from being connected twice.

The pre-release code checkpoint is
`checkpoint/pre-multiaccount-20260731-1fcc43f`. A verified encrypted PostgreSQL
and Render configuration package was captured at the same point. If workspace 2
has received real data, rollback must restore that database package together
with the checkpoint code; an Alembic downgrade alone is not a complete rollback.
