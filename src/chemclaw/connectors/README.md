# `chemclaw.connectors` — the capability seam

**Responsibility:** the one way a capability is added. Every tool the agent can call, every
durable job it can launch, and every skill scoped to a capability arrives as a **bundle** — one
directory here, declared by a `connector.yaml` (D-109, D-118). Adding a capability is adding a
directory; it is not editing core.

## What a bundle holds

| File | What it is | Required |
| --- | --- | --- |
| `connector.yaml` | the manifest: tools, jobs, health, `module:callable` pointers | yes |
| `app.py` | the FastAPI transport, in `server/` — `/healthz` + `/mcp`, built by `connector_app()` | if **we** host the server |
| `tools.py` | the `FastMCP` instance, in `server/`: the argument names, defaults and docstrings the agent sees | if **we** host the server |
| `worker.py`, `workflows.py`, `activities.py` | the Temporal half, on the bundle's own queue | if it owns durable work |
| `skills/<name>/SKILL.md` | judgment that belongs to *this* capability and deploys with it | optional |

The queue is deliberately absent from the manifest: it is `connector-<name>`, derived at dispatch,
because a bundle's worker serves only what the bundle's own modules registered (D-150).

**The variance is information.** `calc` has workflows, activities and a worker; `molfp` has only a
server. That says which capabilities own long-running work, so do not flatten it into a uniform
template. (`chem` used to be the second half of that sentence and is now the *next* paragraph's
example instead: its server moved to `Chemclaw3-mcp` wholesale, so the bundle here has neither
`server/` file — which is a third shape, not a smaller version of the second.)

**A bundle whose server somebody else runs has neither `server/` file.** It declares an `endpoint:`
like any other, and the deployment says the address is not ours to render
(`connectors.<name>.url` in the chart, D-2026-08-09-a-connector-we-do-not-run). Reached across a
network, it must carry a bearer credential — `HttpEndpoint` refuses `auth: mode: none` for a
non-loopback URL.

## The boundary against `science/`

A bundle is a *surface*, not an implementation. The computation lives in `chemclaw.science`
(`bo`, `calc`, `fingerprints`, `labels`) which imports no Temporal, no MCP and no FastAPI, and is
therefore testable without any of them. That list is checked against the tree
(`tests/test_repo_map.py`): it named `safety`, deleted when
`D-2026-08-15-safety-is-a-tool-not-a-gate` made the hazard screen an ordinary MCP server, and
omitted `labels`, so it was wrong in both directions at once. `connectors/calc/` and
`science/calc/` are a pair, not a duplicate: merging them would put orchestration imports inside
the physics, which is the layering rule `tests/test_layering.py` guards.

Since D-156 this holds without exception — `molfp` and `rxnfp` were the last bundles whose code
lived somewhere else (`chemclaw.mcp`), and their engines are now `science/fingerprints/`.

`chemclaw.mcp` could not simply be called `mcp` at the top level: that name belongs to the
installed MCP SDK (`from mcp.server.fastmcp import FastMCP`) and a sibling package shadows it
(D-016). As a submodule of `chemclaw` it never conflicted — which is why the package spent its
last months named `mcp` under a README insisting the name was impossible.

## Why the manifest is checked, and the incident that says so

`make connector-validate` resolves every `module:callable` string in every `connector.yaml`
against the live code. Nothing else can: they are strings, so `mypy` cannot see them and a stale
one fails in a production worker rather than in CI.

The matching hazard is prose. `mcp_servers/calc/` was a third fingerprint-era server duplicating
this bundle's tool surface — two live definitions of `predict_pka`, differing in one of them.
D-113 decided to delete it; it was **actually** deleted in D-117, and the gap is the point: a
README asserted the deletion across four ADRs while the file was still tracked, still built into
the image by `deploy/Containerfile`, and still dispatchable as `CHEMCLAW_COMPONENT=mcp-calc`.

**A README is not a gate.** `tests/test_deploy_chart.py` now asserts the chart↔entrypoint
correspondence in both directions, which is what would have caught it. D-156 found the same shape
once more, in `deploy/README.md`, which was still listing an `mcp-molfp`/`mcp-rxnfp` component that
neither the entrypoint nor the chart has known about for months.

## Capability, not judgment

A connector *computes* — a fingerprint, a pKa, a hazard screen. Whether a Tanimoto score counts as
precedent, or which calculation to run, is a Skill (`skills/` at the repository root, or the
bundle's own). Keeping those apart is gate G6.
