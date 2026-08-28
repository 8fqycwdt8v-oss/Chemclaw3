# D-2026-08-27-the-backend-is-told-who-is-asking — the calculation server stops logging `actor=- session=-`

## Status

Accepted. Third finding of the same blind-spot audit as
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`.

**The transport half was reached independently and first**, by
`D-2026-08-27-a-job-that-fails-leaves-no-row`, which arrived on `main` while this branch was in
flight and passes `turn_identity_hook` into `open_session` as a `request_hook`. That design is the
better of the two and is the one recorded below: this branch had built a second, locally-defined
origin-scoped hook over a shared `core/http.same_origin`, which is a second copy of a security
control — the very thing the reused hook exists to prevent. It is dropped, `same_origin` with it
(one caller left, and its own docstring named two). What is this ADR's alone is the **durable
half**, which that change did not find: the ambient identity the hook reads did not exist on the
job path at all.

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

**One hook, passed in rather than reached for.** `open_session` takes a `request_hook` parameter
and installs it on the `httpx` client it builds; `calc_session` supplies
`connectors.identity.turn_identity_hook(settings.calc_server_url)` — the *same* hook
`connectors/registry.connector_http_client` installs, never a second one. That hook already
produces exactly the set that was missing: the actor, the session, the correlation id, the dry-run
flag and the W3C `traceparent`. A second builder in `core` is the failure this repository already
records as two live definitions of one thing; and `core` may import no sibling
(`tests/test_layering.py`), so the parameter is what keeps one definition and one direction.

**A hook rather than connection headers, and the reason is that the guard travels with it.**
`short_connect_client` follows redirects deliberately (an ingress redirecting `/mcp` to `/mcp/` is
ordinary), an httpx hook runs on every hop, and httpx builds each hop from the previous request's
headers, dropping `Authorization` alone. So identity attached to this connection would be handed to
a redirecting server along with everything else — precisely the hazard `turn_identity_hook`
documents and *removes* for (removing on a foreign hop, not declining to re-add, because the copies
arrive anyway). Reusing that hook is what makes the origin guard one control rather than two: a
second copy is how one of them stops matching `connectors.identity.STAMPED_HEADERS`.

Reading the ambient per request is truthful here for the same reason it is on a connector: the
transport's tasks inherit the context of whoever opened the connection, and `open_session` opens a
session **per call**, so that context belongs to exactly one caller from `initialize()` onwards.

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

It is also **not** superseded by `durable/interceptor.py`, which now binds these same ambients for
every activity on every worker. That interceptor reads argument *models* and skips plain strings
outright, so a model-authored payload can never supply an identity; measured against the real
argument shape, `activity_context([spec, "chemist-1", "job-corr-1"])` binds nothing. The actor and
the correlation id arrive here as bare arguments for exactly that reason — `spec` is the payload
whose digest is the cache key — so this bracket is the only producer on this path.

**The reaction labeller is deliberately left anonymous.** `chemclaw.ingest` may not import
`chemclaw.connectors` (`tests/test_layering.py` allows `ingest -> core|kg|retrieval|science` and
nothing else), so it cannot reach `turn_identity_hook`. It is also a scheduled drain with no turn behind
it, so what it would send is a dry-run flag and nothing else. Making it possible means moving the
builder — and the dry-run ambient it reads — down into `core`, which is a bigger move than this
finding justifies. That is the trigger if a second such caller ever appears.

## Consequences

- `tests/test_connector_transport.py` proves the headers over the wire, against a served app on a
  real port through the real `calc_session`, exactly as the connector's own contract is proven —
  a header contract is only real if the bytes land. Beside it, one test asserts the wiring that has
  no other cover: that the `request_hook` `open_session` is handed actually reaches
  `httpx.AsyncClient(event_hooks=…)`, checked through the real hook so the origin guard is proven
  in both directions on this transport. Both were verified to fail with that one line removed.
- `tests/test_calc_jobs.py` proves the durable half at both ends: the workflow hands the activity
  the memo's actor and correlation id, and the activity's outbound calls carry them; two further
  tests pin that the stamp is removed at the end of the job (a worker runs the next job in the same
  process) and that a run with no memo stamps nothing.
- An incident on an hours-long CREST search can now be joined to the turn, the chemist and the run
  that caused it, on the same keys as everything else.
