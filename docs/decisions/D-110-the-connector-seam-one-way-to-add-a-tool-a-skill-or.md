# D-110 — The connector seam: one way to add a tool, a skill, or an agentic workflow

**Context.** Five extension seams existed, and adding a capability meant touching four unrelated
places — a `@tool` function in `agents/`, a `settings.mcp_servers` entry, a bespoke Temporal adapter
plus a hand-maintained worker list, and a `SKILL.md` folder — three of them Python edits to
orchestration code. A *capability* was a concept the codebase could not name, and its dependencies
(`rdkit`, `torch`, `bofire`, `tblite`) all lived in the chat service's image because tools ran
in-process. `deploy/helm/.../deployment-mcp.yaml` had anticipated the fix and sat inert since F6.

**Decision.** A capability is a **bundle**: `connectors/<name>/connector.yaml` declaring everything it
contributes — the MCP tools its own FastAPI server serves, the durable jobs its own Temporal worker
runs, the skills that teach them, the agent profiles they enable. Discovered by folder (as skills
are), validated by a pydantic manifest with `extra="forbid"` (as `SKILL.md` frontmatter is), enabled
by one config token (as data sources are), gated by `make connector-validate` in CI (as notes and
skills are). No new vocabulary: registry + discriminated union + filesystem discovery + enable-token,
exactly the four shapes D-081 settled on.

`mcp_servers` and its three spec models are **removed**, not deprecated — two mechanisms for
registering a capability is the problem this solves, so keeping one as a compatibility path would
preserve it. `molfp`/`rxnfp` are re-hosted as HTTP connector bundles over their existing FastMCP
capability (`mcp_servers/` unmoved), and the Helm chart now computes `CHEMCLAW_CONNECTOR_URLS` from
the same `.Values.connectors` block that creates the Services, so the addresses the front door dials
cannot drift from the pods that exist.

**Durable jobs: core wrapper over a connector-owned workflow.** `ConnectorJobWorkflow` keeps every
obligation that must not vary per capability — the idempotent workflow id (D-011), `require_actor`
attribution (F4-T3), the PR-gate publish through the *existing* `publish_memory_note_activity`, and
session push-back (F3-T3) — while the connector owns the workflow and its worker, addressed by
**workflow type name + task queue** as strings from the manifest. Core imports nothing from a bundle,
and moving a workflow between workers is a one-line manifest change. The contract is one envelope
(`ConnectorJobResult`: summary, data, optional `Note`), and typing the note as the existing frozen
`Note` means a connector's proposal passes the graph's own validators at the boundary.

A `jobs:` entry declares its arguments either inline (closed scalar types → a generated pydantic
model → a real typed schema) **or** by a `module:Attribute` reference to an existing model. The second
is what makes "any tool" true rather than aspirational: `CampaignSpec` nests a discriminated
optimization problem that YAML cannot re-declare without losing the structure that makes the model
call it correctly — and re-declaring it would be a second source of truth for a schema that already
exists in code.

**The four existing bespoke adapters are deliberately not migrated.** `submit_qm_job`,
`request_development_report` and `start_optimization_campaign` wrap workflows returning typed domain
results their callers consume, not the envelope. Converting them now means either changing three
tested durable workflows' return types — orphaning in-flight histories for no functional gain — or
stacking a third wrapper layer. In Stage C their code moves into its bundle and the moved workflow
returns the envelope directly: one change instead of two. Until then core durable capabilities and
connector durable capabilities coexist by design, and the generic path is what every *new* one uses.

**Two findings from measurement, which changed the design.** Both are recorded because both look
settled from the API surface and are not.

1. **MAF's `header_provider` does not work over streamable HTTP.** It is invoked, with the right
   values, and the server receives nothing: MAF passes the headers through a `ContextVar` set in
   `call_tool` while the request is issued by the MCP transport's `post_writer` task, created when the
   connection opened. A request hook on our own `httpx.AsyncClient` runs *in* that task and works.
   (MAF documents the sibling trap for auth itself: provider headers are absent during
   `session.initialize()`, so a credential passed that way 401s at connect. Auth is an `httpx.Auth`
   on the same client.)
2. **Connectors must be built per turn, not per process.** A connection's transport tasks inherit the
   context of whoever opened it, so connect-time identity is only truthful if a connection belongs to
   one turn. Probing the shared-object shape showed it is worse than inaccurate: **two concurrent
   turns over one connector tool deadlock** — a pre-existing hazard on the stdio path too, since
   `run_turn` has always entered process-lived tools' contexts per turn. So connectors are not
   attached by `build_agent`; `connector_tools()` builds them per turn and the caller passes them to
   `Agent.run(tools=…)`. One fix for both problems, pinned by a concurrency test.

**Consequences.** The identity headers are advisory and stay that way: audit and per-tool authz run in
core before a call leaves the process, and a NetworkPolicy restricts connectors to Chemclaw's own
pods — a connector must never make an access decision on a header's word. The agent-facing `tools`
allow-list is read/compute-only *by validated contract* (`make connector-validate` refuses a mutating
name), so mutation stays on the job path or the core PR-gate tools. An unreachable connector costs its
tools and not the turn, reported by `/readyz` and the `chemclaw_connectors_unhealthy` gauge;
`connectors_required` inverts that to fail-fast for a deployment that prefers not serving.
`SkillManifest` drops `mcp_servers` for the finer-grained `tools`, validated against the whole surface
including out-of-process tools — the coarser field would have passed while the tool it taught was gone.

Design, staging and open questions: `docs/connector-plan.md`. Supersedes the `mcp_servers` half of
D-029 and D-081's transport union; the rest of D-081 stands.
