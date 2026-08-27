# D-2026-08-27-the-backend-is-told-who-is-asking — the calculation server stops logging `actor=- session=-`

## Status

Accepted. Third finding of the same blind-spot audit as
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`.

## Context

Two transports leave this process carrying a tool call, and only one of them said who was asking.

A **connector** is dialled through `connectors/registry.connector_http_client`, whose request hook
(`connectors/identity.turn_identity_hook`) stamps `X-Chemclaw-Actor`, `-Session`,
`-Correlation-Id`, `-Dry-Run` and `traceparent` on every request, origin-scoped, with careful
reasoning about absent-versus-empty.

The **calculation backend** is not a connector. `science/calc/store.py::cached_compute` calls
`connectors/calc/remote.py::calc_session` on a cache miss, which opens an MCP session through
`core/mcp_session.py::open_session` — and that built exactly one header:

```python
headers = {"Authorization": f"Bearer {token}"} if token else None
```

So the fleet's heaviest and most incident-prone server — GFN2-xTB relaxations, Hessians and CREST
searches measured in minutes to hours — logged `actor=- session=-` on every request, while a
two-millisecond property lookup through a connector was fully attributed. `Chemclaw3-mcp`'s
`mcp_server_kit` reads those headers on every request and its `tests/test_identity_contract.py`
holds their spelling; there was simply nothing to read. The reaction labeller
(`ingest/labels/labeller.py`) uses the same primitive and was equally anonymous.

**And on the durable path there was nothing to send even if it had asked.** `CalcJobWorkflow` passed
its activity the bare spec. `ConnectorJobWorkflow` puts `requested_by` and `correlation_id` on the
child's **memo** for exactly this purpose (D-118) — `connectors/bo/workflows.py` reads it for its
campaign record, and its docstring even claims "`connectors/calc/workflows.py` has read the same
memo in production since D-114", which had stopped being true. So no ambient identity existed inside
`run_xtb_calculation` at all: measured, `turn_headers()` at the moment of the remote call carried no
actor and no correlation id.

## Decision

**One header builder, passed in rather than reached for.** `open_session` takes an
`identity_headers` parameter and merges it under the bearer (credential last, so a caller cannot
displace it). `calc_session` supplies `connectors.identity.turn_headers()` — the *same* function the
connector hook uses, never a second one. A second builder in `core` is the failure this repository
already records as two live definitions of one thing; and `core` may import no sibling
(`tests/test_layering.py`), so the parameter is what keeps one definition and one direction.

**Connection headers are honest here and would not be on a connector.** A connector session is held
open for a whole turn, so its identity must be stamped per request by a hook that runs in the
transport's own task. `open_session` opens a session **per call**, which is why the connection's
headers are that caller's from `initialize()` onwards.

**The redirect hazard comes with them, so the guard comes with them.** `short_connect_client`
follows redirects deliberately (an ingress redirecting `/mcp` to `/mcp/` is ordinary), an httpx hook
runs on every hop, and httpx builds each hop from the previous request's headers, dropping
`Authorization` alone. Attaching identity to the connection without a guard would hand a redirecting
server the caller's Entra object id — precisely what `turn_identity_hook` documents. `open_session`
therefore installs an origin-scoped request hook that *removes* the caller's headers on a foreign
hop (removing, not declining to re-add: the copies arrive anyway).

"Same origin" now has one definition, `core/http.py::same_origin`, used by both transports.
`connectors/identity.py`'s private `_origin` is deleted in favour of it — two transports asking one
question must not be able to answer it differently, which is the same argument `is_loopback_url`
already sits in that module for.

**The durable path is threaded explicitly, off the memo.** `CalcJobWorkflow` reads
`workflow.memo_value("requested_by", …)` and `("correlation_id", …)` and passes both as activity
arguments — not in `spec`, which is the model-authored payload whose digest is the cache key, so
identity must not be able to change either. `run_xtb_calculation` stamps them ambient for the whole
dispatch (`_acting_for`), which is what `turn_headers()` then reads. Both default to empty so a run
already in flight, and any direct caller, still decodes and simply stamps nothing: absent identity
stays absent rather than becoming a placeholder.

`_acting_for` is written out rather than shared with
`durable/template_activities.py::_acting_as`, which binds a `StepIdentity` including roles and a
session. This path has neither — the memo carries the actor and the correlation id, and the
conversation stays core's to join through `job_records`, the same boundary
`connectors/bo/activities.py` states.

**The reaction labeller is deliberately left anonymous.** `chemclaw.ingest` may not import
`chemclaw.connectors` (`tests/test_layering.py` allows `ingest -> core|kg|retrieval|science` and
nothing else), so it cannot reach `turn_headers`. It is also a scheduled drain with no turn behind
it, so what it would send is a dry-run flag and nothing else. Making it possible means moving the
builder — and the dry-run ambient it reads — down into `core`, which is a bigger move than this
finding justifies. That is the trigger if a second such caller ever appears.

## Consequences

- `tests/test_connector_transport.py` proves the headers over the wire, against a served app on a
  real port through the real `calc_session`, exactly as the connector's own contract is proven —
  a header contract is only real if the bytes land — and pins the origin guard in both directions.
- `tests/test_calc_jobs.py` proves the durable half at both ends: the workflow hands the activity
  the memo's actor and correlation id, and the activity's outbound calls carry them; two further
  tests pin that the stamp is removed at the end of the job (a worker runs the next job in the same
  process) and that a run with no memo stamps nothing.
- An incident on an hours-long CREST search can now be joined to the turn, the chemist and the run
  that caused it, on the same keys as everything else.
