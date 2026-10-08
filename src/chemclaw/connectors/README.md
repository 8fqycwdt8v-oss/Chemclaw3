# `chemclaw.connectors` — the capability seam

**Responsibility:** the one way a capability is added. Every tool the agent can call, every
durable job it can launch, and every skill scoped to a capability arrives as a **bundle** — one
directory here, declared by a `connector.yaml` (D-109, D-118). Adding a capability is adding a
directory; it is not editing core.

## What a bundle holds

| File | What it is | Required |
| --- | --- | --- |
| `connector.yaml` | the manifest: tools, jobs, health, `module:callable` pointers | yes, unless the fleet owns it |
| `app.py` | the FastAPI transport, in `server/` — `/healthz` + `/mcp`, built by `connector_app()` | if **we** host the server |
| `tools.py` | the `FastMCP` instance, in `server/`: the argument names, defaults and docstrings the agent sees | if **we** host the server |
| `worker.py`, `workflows.py`, `activities.py` | the Temporal half, on the bundle's own queue | if it owns durable work |
| `skills/<name>/SKILL.md` | judgment that belongs to *this* capability and deploys with it | optional |

The queue is deliberately absent from the manifest: it is `connector-<name>`, derived at dispatch,
because a bundle's worker serves only what the bundle's own modules registered (D-150).

**A heavy tool may wait for a slot instead of being refused.** An endpoint's `queued:` names tools
whose calls go through `connector-<name>-interactive` rather than the turn's own session: the
adapter's tool interceptor (`queued.py`) starts a `QueuedToolWorkflow` (`queued_workflow.py`), the
connector's interactive worker (`interactive_worker.py`, sized to the server's slots) makes the call
(`queued_call.py`), and the turn waits `inline_wait_seconds` for it before handing back a job id. It
needs no file in the bundle — one manifest block and one chart entry
(`connectors.<name>.interactive`) — and it applies to a server somebody else runs as well
(`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`).

**The variance is information.** `bo` and `calc` have workflows, activities and a worker; `molfp`
and `rxnfp` have only a server; `results` has jobs and no server. That says which capabilities own
long-running work and which are served elsewhere, so do not flatten it into a uniform template.

**A connector whose server somebody else runs has no bundle here at all.** The fleet owns its
`connector.yaml` and ships it in the `chemclaw-contracts` package, which is the first directory of
`connectors_dir`; the deployment says the address is not ours to render (`connectors.<name>.url` in
the chart, D-2026-08-09-a-connector-we-do-not-run). Reached across a network, it must carry a
bearer credential — `HttpEndpoint` refuses `auth: mode: none` for a non-loopback URL. What stays
here for such a connector, if anything, is a directory of its own name holding only `skills/` (and
a README saying so): judgment is architecture layer 3, and it is found by connector name, so it
loads beside a manifest this tree does not hold. **A connector name declared in two directories is a
startup error**, naming both files, so nothing here can shadow the fleet's manifest.

**And a bundle may declare itself off by default.** `default_enabled: false` is read in exactly one
place — what an *empty* `connectors_enabled` means — because declaring a capability and binding it
are different decisions with different prices
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). Declaring is nearly
free and is what lets a validator resolve a tool name and a `SKILL.md` name the tools it is
judgment about; binding puts every one of that bundle's schemas ahead of the system message on
**every** model call, which `tests/test_context_floor.py` charges to `PREFIX_BOUND` and
`core/config/agent.py` turns into both compaction thresholds. The five process-development bundles
(`props`, `thermalsafety`, `kinetics`, `unitops`, `suitability`) and `pyexec` take that shape, the
flag being in the fleet's manifest. An explicit
enable-list is deliberately **not** filtered by the flag: it says what silence means, not what a
deployment may ask for, and a bundle no configuration could reach would be a control whose
condition cannot occur.

That split is also why the validators and the runtime read different sets. `connector_tool_names`
and `skills_dirs` answer "what can this turn call"; `declared_connector_tool_names` and
`declared_skills_dirs` answer "what does this tree declare", and the second pair is what
`skill-validate`, `prose-validate` and `template-validate` use — so an opt-in bundle's own skill is
still read by CI on a checkout that never binds it.

## The boundary against `science/`

A bundle is a *surface*, not an implementation. The computation lives in `chemclaw.science`
(`bo`, `calc`, `fingerprints`, `labels`) which imports no Temporal, no MCP and no FastAPI, and is
therefore testable without any of them. That list is checked against the tree
(`tests/test_repo_map.py`). `connectors/calc/` and `science/calc/` are a pair, not a duplicate:
merging them would put orchestration imports inside the physics, which is the layering rule
`tests/test_layering.py` guards. `molfp` and `rxnfp` follow the same split, with their engines in
`science/fingerprints/` (D-156).

## Why the manifest is checked, and the incident that says so

`make connector-validate` resolves every `module:callable` string in every `connector.yaml`
against the live code. Nothing else can: they are strings, so `mypy` cannot see them and a stale
one fails in a production worker rather than in CI.

The matching hazard is prose: a third, fingerprint-era `calc` server outlived the ADR that deleted
it — still tracked, still built into the image, still dispatchable — while a README asserted it was
gone (D-113, D-117). **A README is not a gate.** `tests/test_deploy_chart.py` asserts the
chart↔entrypoint correspondence in both directions, which is what would have caught it.

## The core modules beside the bundles

| Module | What it is |
| --- | --- |
| `manifest.py` | the `connector.yaml` model — one validated contract for everything a bundle contributes |
| `registry.py` | discover bundles, validate them, and build what the agent advertises |
| `transport.py` | how a connector is reached, so an unreachable one degrades instead of failing the turn |
| `health.py`, `reachability.py` | the startup probe behind `/readyz` and `/metrics`, and what this process last learned about each connector |
| `jobs.py` | one generated agent tool per declared job |
| `queues.py` | the one spelling of a bundle's `connector-<name>` queue |
| `queued.py`, `queued_workflow.py`, `queued_call.py`, `interactive_worker.py` | the interactive queue above |
| `identity.py`, `caller.py` | the connector's own credential, and the advisory caller identity a tool can read |
| `server.py`, `server_entry.py` | wrap a `FastMCP` capability as the FastAPI app a bundle serves, and run one as a process |
| `worker.py` | run one bundle's own Temporal worker |

## Capability, not judgment

A connector *computes* — a fingerprint, a pKa, a hazard screen. Whether a Tanimoto score counts as
precedent, or which calculation to run, is a Skill (`skills/` at the repository root, or the
bundle's own). Keeping those apart is gate G6.
