# D-2026-08-06-a-connector-that-authenticates-nobody — A connector that authenticates nobody

**Status:** accepted · **Date:** 2026-08-06

## Context

Two open `BACKLOG.md` rows, filed by the whole-codebase security sweep as separate findings:

- **[M] Every shipped connector is unauthenticated.** All seven manifests ship `auth: mode: none`,
  which leaves the ingress NetworkPolicy as the only control. The `bearer` mode exists and is
  unused, and a connector serves its *whole* FastMCP surface — `allowed_tools` is a client-side
  filter.
- **[M] The unauthenticated `X-Chemclaw-Actor` header becomes durable GxP attribution.**
  `CallerLogMiddleware` documents the identity headers as advisory, but a bundle's tool stamps them
  into `bo_campaigns`/`bo_suggestions`, so anything that can reach the pod can forge who ran an
  experiment.

They are one finding. The second row itself says so — *"the root fix is connector authentication"* —
and the narrower fix it proposes (attribute from the workflow payload's `requested_by`) had already
landed for the durable path in `D-2026-08-06-the-memo-already-carried-the-actor`. What was left open
is the **inline** path, `suggest_next_experiment` on the `bo` connector, which has no workflow and
therefore no memo: it reads `caller_provenance()` and writes it into `bo_campaigns.opened_by`.

There is no memo to read there and there should not be one. The question is not where an inline tool
finds a trustworthy actor; it is why a header on a connector request was ever untrustworthy in a
deployment that talks only to its own pods.

Everything below was measured on the shipped configuration.

## Decision

### `auth: mode: none` was half a sentence

The manifest's auth block describes how **core authenticates outward** to a connector. Nothing in
the system described what a connector requires **inward**. `NoAuth`'s own docstring reads:

> No credential — the connector is inside our own trust boundary. […] `ConnectorManifest` refuses it
> for a non-loopback URL unless the deployment has explicitly opted into insecure binding

Both halves were false. No such validator existed (the sweep filed that separately, as prose
asserting what the code does not do), and nothing enforced the boundary the first sentence asserts.
So in the shipped chart — where `connector_urls` moves every bundle onto an in-cluster Service — a
connector accepted any request that reached its port, served every tool including the index and
write tools deliberately kept off `allowed_tools`, and let the caller choose the `X-Chemclaw-Actor`
value a bundle records as durable attribution.

### One credential, named by configuration, required by both halves

`CHEMCLAW_CONNECTOR_TOKEN_ENV` names the variable holding the fleet's shared connector credential.
Set it and both halves of the boundary become real:

- **Server** — `RequireConnectorCredential` refuses any request without
  `Authorization: Bearer $<var>`, compared with `secrets.compare_digest`.
- **Client** — `auth_for` sends it to any connector whose manifest declares `mode: none`, which is
  exactly the set that declares itself to be inside our boundary. A connector declaring its own
  `bearer` keeps it: that is the third-party case, and sending our fleet token to someone else's
  server would hand out the credential this exists to protect.

The indirection through a *variable name* rather than a value is `manifest.BearerAuth`'s existing
idiom, not a second one: the value is read per request, so rotating the secret needs no restart, and
no credential is ever written into a manifest or a ConfigMap.

### A deployment cannot become an open fleet by omission

The credential is optional, because a loopback dev fleet's boundary is the machine and requiring a
token there would be ceremony. That reading is exactly what a cluster must not be able to inherit
silently, so `require_secure_channel` states the two ways to be legitimate — reach the connector over
loopback, or send it a credential — and refuses anything else:

```
SECURITY: connector 'bo' is reached at 'http://chemclaw-connector-bo:8080/mcp' — not loopback —
with no credential, so anything that can reach it may call its tools and may name any actor in the
identity headers a bundle records. …
```

`CHEMCLAW_SERVICE_ALLOW_INSECURE` is the same explicit, logged opt-out the front door already offers
for its own unauthenticated mode (SEC-2), and it warns per connector rather than trusting silently.

It is judged on the **effective** URL — after `connector_urls` — which is why it is a function called
from `connector_http_client` and not the manifest validator `NoAuth`'s docstring promised. A manifest
ships a loopback dev default and the deployment is what moves it, so the manifest alone cannot know
whether its own `mode: none` still describes a boundary that exists. That is also the correction to
the docstring: the check it claimed could not have worked where it claimed to be.

Two call sites, one rule: `connector_http_client` covers every process that reaches a connector by
construction, and `check_connectors_at_startup` runs the same function over the enabled set so a
misconfigured deployment fails at boot rather than on a chemist's first turn.

### `/healthz` and `/metrics` stay open

The kubelet's probe carries no credential and neither does a Prometheus scrape. Both were already
reachable to anything on the pod network, and neither serves a tool, reads an identity header, or
writes a row — so requiring a credential there would trade a real liveness signal for no property.

### The tool allowlist is deliberately *not* enforced server-side

The obvious companion to this change is to make `allowed_tools` more than a client-side filter. It
would be wrong. That list is *the agent's* subset, and the ingestion path legitimately calls index
tools outside it — `connector_app`'s own docstring says so, and `molfp` serves `index_molecule` for
exactly that reason. One surface with two legitimate clients wants authenticating, not partitioning;
partitioning it by the agent's list would break ingest while calling itself a fix. Authentication is
what actually closes the row: the surface is no longer callable by anything that reaches the port.

### The other declared-and-unwired identity control, from the same lane

`map_to_hpc_identity` had **no caller anywhere in `src/`** ([L] in the same backlog lane). §7.2 makes
the oid → HPC-identity mapping a compliance requirement rather than an implementation detail: the
cluster runs every user's job under one shared service identity and never sees an Entra oid, so that
log line is the only link back to the chemist. It was declared, documented, unit-tested — and never
written, which is the shape `D-2026-08-05-a-declaration-outliving-what-it-describes` names.

It is now called in `submit_to_hpc`, on both the Nextflow and mock paths, before the launch rather
than after: a submission that fails still consumed the intent, and a mapping recorded only on
success would be missing exactly for the runs someone asks about.

The mapping also lands on `HpcJobHandle.run_as` instead of only in a log line. A log is pruned by
whatever retention the log store has; a field on the handle is in Temporal history beside the
`requested_by` memo it maps from, which is where the rest of a run's attribution already lives. The
launcher is told nothing new — it authenticates our service credential and infers the identity from
it — so this records what happened rather than asserting a field on someone else's API.

`test_hpc_bridge.py`'s existing test is why this needed finding at all: it called the function
itself, so it passed for as long as nothing else did. The new test drives the *submission* path,
which is the repository's own recorded lesson about a test supplying what the system was supposed to
supply.

### Three defects the review of this change found, none of which a test had caught

Recorded because each is a shape worth recognising rather than an incident: all three are the *new
control* failing in a way that reads as the system working.

1. **The probe exemption matched one spelling of its own path.** `/healthz` and `/metrics` were
   exact-matched, and a connector app is served at the root in a cluster but mounted under `/<name>`
   by the dev composite (`cli.connectors_dev`). Every production test passes; the day someone runs
   the composite with a credential configured, every kubelet probe 401s and reads as "the connectors
   are down". Matched on the last path segment now, which is permissive only toward paths that exist
   solely under a mount — no MCP endpoint ends in either name.

2. **A refused request was logged nowhere.** `CallerLogMiddleware` sits *inside* the credential
   check and so never runs for a 401, and the middleware itself returned a bare response. An
   unauthenticated caller sweeping a connector's port left no trace at all — on the one process
   family whose entire surface is capability. It now logs at WARNING with the claimed actor, which
   is the "someone unauthenticated claimed to be X" line an operator wants: from outside the
   boundary, therefore logged and never believed.

3. **A pod that could serve nothing reported itself healthy.** With the variable named but unset,
   the middleware correctly 503s every call while `/healthz` still answered `{"status": "ok"}` — so
   the pod stayed in rotation and the front door's `/readyz`, which reads exactly that route, agreed.
   The route now reports `credential-unavailable` with the variable's name. Reported rather than
   crashed: mounting the Secret fixes it without a restart, and a CrashLoop would bury the line that
   says what is wrong.

### The credential also had to join the redaction inventory

`core.logging`'s `_SECRET_SETTINGS` matches settings whose *value* is the secret, and the arm beside
it enumerates manifest-declared `BearerAuth.token_env` names. This credential is neither: its value
is a variable **name**, and no shipped manifest declares a bearer. So the fleet token sat outside
every arm of the inventory the moment it was introduced. It is read there now, per call, so a
rotated token stays covered.

### What the credential does and does not establish

It authenticates the *process* — that the caller is core, holding the fleet's token — not the end
user, whose identity core validated before the call left it. That is the chain `audit_events`
already rests on, one hop further out: core vouches for the chemist, the credential vouches for
core. What it replaces is a chain with no first link at all. The headers remain advisory for
*authorization* — a connector still never gates on one, because that question was answered upstream
against a validated token.

## Consequences

- A seventh plain secret, `CHEMCLAW_CONNECTOR_TOKEN`, argued in `test_helm_chart.py` where the
  other six are. It is the only one not about reaching outward, and federation cannot supply it:
  it is presented on an in-cluster call between our own pods, the case workload identity does not
  cover. Its polarity is the four-key one — absent, the front door refuses to start rather than
  degrading quietly.
- A connector pod whose secret is missing while the deployment names one answers **503** rather than
  serving open. The failure mode being excluded is the one that reads as working: the client sends a
  credential nobody checks.
- Dev is unchanged. `make connectors` binds `127.0.0.1`, `connector_token_env` defaults empty, and
  every existing test that builds a connector app or client keeps passing.
- `test_chart_config_keys_have_a_consumer` gained a third way for a chart key to be consumed:
  a variable some `*_env` setting *names*. Derived from the chart's own values, like the shell
  exemption beside it, so renaming the variable keeps the exemption and a later `*_env` setting is
  covered the day it appears.
- Both controls are mutation-proven: removing the middleware registration fails three tests,
  removing the startup refusal fails two, and the ten tests that pin *unchanged* behaviour keep
  passing under both mutations — which is what says they are testing the fix rather than the fixture.

## Alternatives rejected

- **Declaring `bearer` in all seven manifests.** The credential is a deployment fact, not a
  capability's. Writing it into seven repo files makes the dev fleet need a token to run and states
  the same deployment decision seven times; `connector_urls` already established the shape for
  "manifest ships the dev default, deployment overrides".
- **A manifest validator, as `NoAuth`'s docstring promised.** It cannot see the effective URL, so it
  would pass on every shipped manifest (all loopback) and catch nothing in the cluster where the
  exposure is.
- **Scoring each record's trustworthiness at the write.** A per-record trust flag would be a second,
  weaker answer to a question the transport answers once, and would be unreachably false by
  construction once the channel refuses unauthenticated callers — dead machinery guarding a door
  that is already locked.
- **mTLS between core and connectors.** Stronger, and it belongs to the service mesh rather than to
  this repo: it needs a CA, issuance and rotation that the cluster owns. A bearer credential is what
  can be shipped and verified offline today, and it does not foreclose mTLS later.

## Related, not fixed here

The connector `entra_workload` / `entra_obo` auth modes still need the real tenant that blocks every
other live Entra edge, and the built-in write gate still does not consult the connector-declared
`state_changing` set (`BACKLOG.md`, the same lane).
