# D-2026-08-08-a-served-tool-is-a-reachable-tool — the allow-list guarded the agent, not the port

**Status:** accepted

## Context

D-029 keeps mutating tools off the agent's connector surface: the manifest's `tools` allow-list is
what core will call, and `connector-validate` refuses an `index_*`/`write_*` name on it. That
boundary is real and it held. What it never covered is the *port*.

`molfp` and `rxnfp` each served an `index_*` tool that no manifest named. The justification was
written in three places — the two `connector.yaml` headers ("The server still exposes it, for the
ingestion path that runs outside the conversation"), `connector_app`'s docstring ("the same server
can also expose index/write tools for the ingestion path"), and a test docstring in
`test_connector_transport.py` that cited it as the reason not to check the served set at all.

Two things were wrong with it.

**The ingestion path never called them.** Grep across `src/`, `tests/`, `scripts/` and `docs/` finds
the only writers to be `ingest/eln/ingest.py` calling `FingerprintStore.add()` in process under
`ElnSyncWorkflow`. The only other references to the tool names were the manifests, `authz.py`'s
deny-list and two tests asserting they were served.

**A connector authenticates nothing, by design.** `connectors/server.py` says so: the
`X-Chemclaw-*` headers are logged and never trusted, because authorization happened in core before
the call was made, and the boundary is the NetworkPolicy. That argument is sound for the *declared*
surface, which core reaches through an allow-list it controls. It is not an argument for a tool core
does not know exists. Proved by serving the real `connector_app(molfp_server, name="molfp")` and
completing a hand-rolled MCP handshake with **no `Authorization` header**:

```
initialize                 -> 200
tools/list                 -> ['index_molecule', 'similar_molecules', 'substructure_matches']
tools/call index_molecule  -> HTTP 200  isError: False
SELECT ... FROM molecule_fingerprints -> the row is there
```

`molecule_fingerprints` backs `similar_molecules`, `substructure_matches` and
`FingerprintReactionRetriever` in the report path, so the reachable consequence is the agent citing
attacker-chosen SMILES as lab precedent — around the PR-gate, which is the line every other write
into evidence goes through. The chart's own connector-ingress NetworkPolicy admits the monitoring
namespace to that port and concedes in a comment that "the port also carries `/mcp`, so this grant
is not metrics only", so the reachable set is wider than core's own pods.

**Separately, `auth: mode: bearer` was send-only.** `BearerAuth` names an env var,
`connectors/identity.py` sets an `Authorization` header from it, and **nothing verifies it**:
`connector_app`'s signature took no manifest, the string "Authorization" appeared nowhere in
`connectors/server.py`, and `connector-validate` accepted a bearer bundle without comment (measured:
"connector-validate problems for a bearer bundle: NONE"). Every shipped bundle declares
`mode: none`, so it was dead rather than wrong — but a deployment following the manifest's own
advice ("bearer for everything in-cluster") would mount a secret, record the control as enabled, and
serve every tool to anything that could reach it.

## Decision

**A tool a connector serves is a tool anything on the network can call, so the served set and the
declared set are the same set.**

- The two `index_*` tools are deleted. The ingestion path already calls `FingerprintStore.add()`
  directly and loses nothing.
- `connector-validate` gains `_served_tool_problems`: it imports each bundle's server module,
  enumerates the **live** `FastMCP` tool set, and refuses any tool the manifest does not declare.
  This is the only rule in that validator that reads the running server rather than the YAML, which
  is precisely why the gap was invisible to the other four.
- `connector_app` enforces the auth its own manifest declares, via `BearerAuthMiddleware`.

**The comparison is against `tools`, and that is forced rather than chosen.** This is the part worth
recording, because the first attempt got it wrong: `state_changing`/`read_only` look like a wider
"what this bundle serves" declaration, so the rule was written against their union — and
`_check_classification` rejected the test manifest, because it already refuses a manifest that
classifies a tool it does not serve. Both lists are constrained to be subsets of `tools`. **The
schema has no way to express "served but not agent-facing."** The state those three comments
described was never representable, which is why a comment was the only place it could be written
down. A capability that must stay off the agent's surface is a `jobs:` entry — authorized,
dry-run-gated and attributed by core — or a core PR-gate tool. `tests/test_validate_connectors.py`
pins the constraint the rule rests on, so it cannot be relaxed without the rule noticing.

**Bearer is enforced as middleware, not a dependency**, and that is the second thing worth
recording: `/mcp` is `app.mount`ed, and a mount bypasses the enclosing app's dependencies entirely.
A `Depends(...)` would have guarded `/healthz` and `/metrics` — the two routes that need it least —
and none of the surface the credential exists for. Those two stay open, matching the front door's
probe allowlist: a kubelet probe and a Prometheus scrape carry no identity. A missing or empty token
refuses rather than compares, so a half-configured deployment fails closed instead of accepting the
empty string; comparison is `compare_digest`.

The manifest is read from the registry rather than passed in, so all seven `app.py` modules stay one
line and no bundle can forget to wire it. That lookup is failure-tolerant — a bundle must still be
importable when the manifest directory is not readable — and the cost of that tolerance is that an
unreadable manifest serves unauthenticated. That is the pre-existing behaviour, and it is why the
declaration is validated separately in CI: a gate that an unreadable file can disable is not a gate.

## Consequences

The fingerprint corpus is no longer writable over MCP, and the gap cannot reopen silently: adding a
tool to a bundle's server without declaring it in the manifest now fails `make connector-validate`.
The cost is one import of every bundle's server package (~13s across the seven, mostly rdkit and the
ML stack) in a CI gate. That is the price of asking the server instead of the file, and the
isolation it might appear to violate is a *runtime* property of the chat pod
(`tests/test_connector_isolation.py`) rather than of a short-lived validator process.

`auth: mode: bearer` now means something. No shipped bundle uses it, so nothing changes for the
current deployment — which is exactly the state in which to fix it, rather than after an operator
has relied on it.

`index_molecule`/`index_reaction` remain in `DEFAULT_WRITE_TOOL_GATES` (D-068). They name nothing
now, and the entries are harmless, but they are also the cheapest possible defence if either name
ever returns as a job or an in-process tool — and removing a name from a *deny* list is the kind of
edit that is safe until it is not.
