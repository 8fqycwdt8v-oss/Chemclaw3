# D-2026-08-26-an-empty-allow-list-is-not-an-allow-list — an endpoint declares its tools, and a manifest may not turn on the field that executes

## Status

Accepted.

## Context

A fresh whole-tree audit drove a real MCP server through the connector seam and found two ways in.
Both were invisible to the suite: the four connector suites were green (92 tests) before and after
the defects were demonstrated.

**The empty `tools:` list.** `_check_classification` partitions an endpoint's `tools` against
`state_changing` and `read_only`, and its own docstring calls it the control that cannot be wrong
quietly: "a typo matches nothing, an omission reads as harmless, and either ships a write that looks
exactly like a gated one. Refusing to load is the only option that cannot be wrong quietly." That
argument is right, and it had a hole in the shape of its own premise — a partition of *nothing* is
trivially satisfied. An endpoint that simply omitted `tools:` passed the check written to make an
omission loud, and both of the endpoint's guarantees inverted at once:

- `registry` built `allowed_tools=tuple(endpoint.tools) if endpoint.tools else None`, and
  `transport._allowed` read `None` as "everything this server offers", so the *whole* advertised
  surface was bound to the model;
- nothing that arrived was in `state_changing_tool_names()`, which is manifest-derived, so
  `agent.authz.side_effecting_call` answered `False` for every one of them — including a write.

That answer is the input to the plan gate (D-167) and to the dry-run refusal, in the posture the
shipped chart runs (`CHEMCLAW_HARNESS_AUTONOMY: plan_only`). Measured against a live server serving
one tool named `wipe_database`: `allowed_tools: None`, `state_changing_names: []`,
`side_effecting_call('wipe_database') -> False`, and the call returned `wiped yes`. **The manifest
that declared the least got the most**, and `connector-validate` reported nothing, because its
rule 5 only compares against `tools` for bundles with an in-repo `server/` module — which a remote
server, the documented shape for the `Chemclaw3-mcp` fleet, does not have.

**`command:` is the one endpoint field that executes.** A bundle is discovered by existing — any
subdirectory of a `connectors_dir` entry holding a `connector.yaml` — and discovery is enablement
unless `connectors_enabled` narrows it. Neither the manifest model nor the validator had any rule
about `StdioEndpoint.command`: no allow-list, no path check, nothing. So a YAML file appearing on
that path ran its command in the chat process, before the MCP handshake, under the identity holding
every connector bearer token, the Postgres pool and the Temporal client. Measured: a manifest
declaring `command: /bin/sh, args: ["-c", "id > …; exec cat"]` wrote `uid=0(root)` to disk, the
connector was then reported `unreachable`, and the turn proceeded normally. **The spawn happening
before the failure is what made it quiet.**

## Decision

**An endpoint declares at least one tool, and every tool it declares is classified.** The empty list
is refused where the typo already was, in `_check_classification`, so both spellings of "unstated"
fail the same way.

**`allowed_tools` is total.** With no way to build a spec without a declaration, the `None` meaning
"everything this server offers" has no producer, so it is deleted rather than left unreachable:
`ConnectorSpec.allowed_tools` is `tuple[str, ...]`, `_allowed` has no fall-through branch, and
`_narrow_allowed_specs` is an intersection rather than a substitution. This is the half that matters
in a year — refusing the empty list stops the state arising, and making the field total stops it
coming back.

**A manifest may not turn on the transport that executes.** `StdioEndpoint` is refused unless the
new `connector_stdio_enabled` setting (default `false`) allows it. No shipped bundle declares stdio;
it is the zero-infrastructure path for local development and for the transport's own tests, and
those say so explicitly. The refusal is at spec-build time rather than at parse time deliberately,
so `StdioEndpoint` stays constructible — the transport's own tests build one directly — while a
*file* is an inert declaration until an operator turns the transport on.

## Consequences

A third-party manifest that omits `tools:` now fails to load instead of loading with the widest
possible surface. That is the intended cost and it is one line per tool, once — the same cost the
classification rule already charged.

The stdio gate is a setting rather than a deletion because the transport has a real use and no
production caller; deleting it would be the KISS answer to a capability nobody runs, and that is a
separate decision to take on its own merits rather than as a side effect of a security fix.

Two guards this ADR does **not** claim: `connector-validate` still does not check a remote server's
declared surface against what it serves (it cannot, offline), and nothing bounds a connector's
*response* while its request is capped — a 64 MB payload arrived intact. Both are open, and neither
is fixed by the change above.

The comment stating "an undeclared tool is treated as a read" is deleted. It described the hole
rather than the design, which is how it survived: it read as a decision.
