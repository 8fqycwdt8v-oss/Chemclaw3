# D-2026-09-14-identity-stamping-is-cores-not-a-connectors — the header stamp moves to `core`, and the labelling leg stops being anonymous

## Status

Accepted. Closes the `BACKLOG.md` row "the labelling client is the one MCP leg with no identity or
trace on the wire".

## Context

`core/mcp_session.open_session` takes a `request_hook` so a caller can stamp the outbound request.
`connectors/calc/remote.py` passes `turn_identity_hook`, so that leg carries the W3C `traceparent`,
the correlation id, the actor and the session, plus the origin-strip guard that removes them again
on a cross-origin redirect. `ingest/labels/labeller.py` is the **only other** `open_session` caller
and passed nothing: `Authorization` and nothing else, on a drain that runs for hours inside a
durable activity, so the trail stopped at this process boundary.

**It was not one line, and the module that could have fixed it said so in a comment.**
`turn_identity_hook` lived in `connectors/identity.py`, on top of `agent.turn_flags` (the dry-run
flag) and `connectors.manifest` (the credential union). Neither `ingest -> connectors` nor
`ingest -> agent` is an edge `tests/test_layering.py` permits, so the hook was unreachable from the
one caller that needed it.

## Decision — where identity stamping for a non-connector MCP client belongs

**It belongs in `core`, and so does the flag it reads.** The argument is what the code depends on
rather than what it is used by:

- `turn_headers()` reads `core.identity_context` (the actor and correlation id),
  `core.session_context` (the session), `core.tracing` (the W3C context) and one boolean flag. Four
  of the five are already core's; nothing it reads is a fact about connectors.
- The one exception was `agent.turn_flags.is_dry_run`, and that module's *own docstring* compares
  itself to `core.session_context` and `core.identity_context` — the two ambients it sits beside.
  It was in `agent/` because of the order things were written in, and that placement is the whole
  reason the stamp could not move.
- What genuinely is a connector's is `auth_for`: which credential to send is declared in a bundle's
  `connector.yaml`, so it reads `connectors.manifest`. That import is the line.

So: `agent/turn_flags.py` → `core/turn_flags.py`, and the header half of `connectors/identity.py` →
`core/call_identity.py`. `connectors/identity.py` keeps `auth_for`, `_EnvBearerAuth` and
`MissingConnectorCredential`, and its only first-party import outside `core` is
`connectors.manifest`. `ingest/labels/labeller.py` passes
`request_hook=turn_identity_hook(settings.rxnlabel_server_url)`.

**The rejected alternatives**, both of which the BACKLOG row floated:

- *A layering exception for `ingest -> connectors`.* That would encode the accident — the labelling
  server is an endpoint this system dials, not a connector bundle, and letting ingestion import a
  connector package to reach a header function widens an edge for one function's sake.
- *A core-level `trace_and_identity_headers()` that `connectors/identity.py` composes.* This is the
  same answer with a seam in it, and the seam buys nothing: after the move there is no
  connector-specific header left to compose with. The stamp is the whole thing, and one definition
  beats a wrapper (`tests/test_message_pairing.py` scans for a second definition of a shape for
  exactly this reason).

**What the labelling leg gets, and what it does not.** It gets the actor, the session, the
correlation id, the dry-run flag and the trace context, plus the origin-strip guard — so a labelling
server answering `302` cannot harvest the trail. It does **not** get an authorization decision: the
headers are advisory, as they are for every connector, and the server must never decide on them.

## Consequences

`chemclaw_mcp_*` records on the labelling server can now be joined to this repository's audit trail
on the correlation id, and a labelling drain appears under the turn that started it in a trace.

Nothing changes for connectors: the same function, from a different module. Four `src/` importers
and six test files were repointed, and `connectors/identity.py` lost its `agent` import — which
narrows the package graph rather than widening it.

## What keeps it true

- `tests/test_label_enrichment.py::test_the_labelling_leg_carries_the_turn_that_asked_for_it` —
  drives the real `RxnLabelServer` over HTTP against a `FastMCP` on loopback under uvicorn and
  asserts the actor, session and correlation id the tool body sees. Mutation: removing the
  `request_hook=` argument fails it. Driven rather than asserted about the source because
  `core/call_identity.py`'s own docstring records a header mechanism that *is* invoked, with the
  right values, and delivers nothing — only a listener tells those apart.
- `tests/test_connector_identity.py` — the whole existing stamp/strip suite, unchanged in substance
  and repointed at the new module.
- `tests/test_layering.py` — the edges. `connectors -> agent` is no longer needed for this, and
  `ingest -> core` is the edge the fix uses.
- `tests/test_tracing.py::test_both_boundaries_are_actually_instrumented` — reads
  `core/call_identity.py` now, so the trace context still provably leaves the process.
