# Task: the generic connector seam — one way to add a tool, skill or agentic workflow

Requested 2026-07-26. Design + staging in `docs/connector-plan.md` (deep status-quo analysis,
eight interview decisions, two verified MAF API findings). This file is the working queue.

Branch: `claude/generic-connector-tools-workflows-uz8afs`.

## Decisions driving the work

1. Capability tools move out to connectors; the 11 conversation-plumbing tools stay in core by rule.
2. Agentic workflows: declarative `AgentProfile` bundles now; deterministic step templates specified
   and gated (Stage E).
3. Durable jobs: generic `ConnectorJobWorkflow` in core (idempotency, actor, push-back, PR-gate) over
   a connector-owned workflow addressed by **type-name string**.
4. A connector is an in-tree bundle folder + one config enable-token.
5. One FastAPI app per domain; one composite dev process.
6. `X-Chemclaw-*` header contract (advisory only) + per-connector auth union.
7. Unreachable connector ⇒ degrade loudly; `connectors_required` ⇒ fail fast.
8. `mcp_servers` is **removed**, not deprecated.

## Stage A — the seam (core) — DONE

- [x] `connectors/manifest.py` — `ConnectorManifest`, `EndpointSpec` (stdio|http), `ConnectorAuth`
      (none|bearer), `JobSpec`, `JobParam`; all `extra="forbid"`
- [x] `connectors/identity.py` — header provider (reads the ambient ContextVars per call) +
      `httpx.Auth` per auth mode
- [x] `connectors/jobs.py` — generated durable tool factory (params model via `create_model`,
      docstring from the manifest, registered through the existing `register_tool`)
- [x] `workflows/connector_job.py` — `ConnectorJobWorkflow` + `ConnectorJobInput`/`ConnectorJobResult`
- [x] `connectors/registry.py` — discover → validate → enable → build (MCP tools + job tools)
- [x] `connectors/health.py` — bounded startup probe
- [x] `chemclaw/config.py` — `ConnectorSettings`; delete `mcp_servers` + the three MCP spec models
- [x] `agents/chemclaw_agent.py` — assemble from the connector registry; delete `_mcp_tool`
- [x] worker registration for `ConnectorJobWorkflow`
- [x] `/readyz` detail + `chemclaw_connectors_unhealthy` gauge; `connectors_required` fail-fast
- [x] `scripts/validate_connectors.py` + `make connector-validate`; retarget `validate_skills` and
      `validate_prose_contract` off `settings.mcp_servers`
- [x] tests: manifest validation, registry enable/unknown, header provider, auth, generated job tool
      (audit+authz wrap it), `ConnectorJobWorkflow` against a real `WorkflowEnvironment`
- [x] `.env.example`, Helm values/templates, `docs/runbook.md`, `DECISIONS.md` ADRs

Gate A: `make lint type test` green; audit+authz demonstrably wrap a connector-sourced tool and a
generated job tool; unknown enabled connector fails loud; non-loopback `auth: none` refused.

## Stage B — reference bundles + the durable path proven — DONE

- [x] `connectors/molfp/`, `connectors/rxnfp/`: manifest + FastAPI app mounting
      `FastMCP.streamable_http_app()` at `/mcp` plus `/healthz`
- [x] `scripts/connectors_dev.py` + `make connectors` (its composite must run each mounted app's
      lifespan — Starlette does not, and a connector's lifespan is what starts its MCP session
      manager; caught by the transport test)
- [x] fixture connector (`tests/fixtures/connectors/fixture/`) with its own workflow: the durable
      contract proven end to end under a real `WorkflowEnvironment` (skipped offline, like every
      Temporal test here; the wrapper's worker registration and the envelope shape are pinned by
      sandbox-safe tests that always run)
- [~] The four bespoke adapters are **deliberately not migrated** — they wrap workflows returning
      typed domain results, not the envelope. Moves in Stage C with their code (D-092, plan §9).

Gate B met: fingerprint search reached over HTTP with the identity headers *observed by a live
server*, and two concurrent turns proven to keep their own identity.

## Stage C — domain connectors

- [x] **Safety rubric verified across the process boundary** before moving anything
      (`tests/test_connector_safety_rubric.py`): a connector tool call is audited with the turn's
      actor, and `tool_role_gates` denies it by name without the tool body ever running. Neither is
      inspectable from the wiring — MAF assembles MCP tools separately from the configured ones — so
      this had to be driven through a real agent against a real server.
- [x] `safety` — `screen_hazards`, with the `safety-screening` skill in the bundle
- [x] `chem` — `resolve_compound`, `stoichiometry_table`, `green_metrics`, `render_structure`
      (takes `rdkit` out of the front-door image)
- [x] `calc` — the calculators + the calibration ledger, with `calculation-selection` in the bundle
      (takes `tblite` and the calculation store's driver out)
- [x] Helm: an entry per bundle; Deployment/Service/NetworkPolicy already generalized in Stage A
- [ ] `kg` — `find_notes`, `expand_note`, `find_knowledge_gaps`, `gather_evidence`. The deepest
      coupling: it needs the knowledge tree and the vector index, so decide first whether it also
      owns re-indexing (`NoteReindexWorkflow` moves with it or does not).
- [x] `bo` — the reference connector-owned durable capability (D-094): its workflow, activities and
      worker live in the bundle on `connector-bo`, `start_optimization_campaign` is a manifest
      `jobs:` entry, and the bespoke adapter is deleted. Core serves no BO workflow. The move needed
      one manifest entry plus the workflow's return type, and no core edit — the property the seam
      was built to have. `write_campaign_node` is gone: the note *mapping* stayed in the bundle, the
      *publish* moved to core, so a connector structurally cannot reach the PR-gate.
      Added `JobSpec.precondition` so the round-ceiling guard survived the migration (every other
      placement re-runs at replay against current config).
- [ ] `qm`/`report` job manifests, once their workflows move and return the envelope directly

## Stage D — agentic workflow configuration — DONE

- [x] Profiles authored as files (`AgentProfile` Stage 3): `profiles/<name>.yaml` for a profile that
      spans capabilities, `connectors/<name>/profiles/` for one that belongs to a bundle. The stem is
      the name; a `name:` key is refused; `extra="forbid"` makes a typo'd override a startup error
      rather than a silent no-op.
- [x] `POST /sessions {profile}` + one cached agent per profile, with the profile fixed for the
      session's life and carried on the live-session record so the turn gets the matching agent
      *and* the matching connector set.
- [x] `profiles/property-lookup.yaml` — a real worked profile, not a placeholder.
- [~] Profile-name RBAC gate: deliberately not built. A profile can only attenuate, so gating the
      *name* protects nothing that `tool_role_gates` does not already protect at call time; it would
      be usability, and there is no caller asking.
- [~] A rehydrated session returns on the default profile (the owner row does not record it).
      Documented in the runbook; persisting it is a migration, and the degradation is to the *full*
      surface rather than a wrong one.

## Stage E — step templates

- [ ] Specified only. Trigger recorded in `BACKLOG.md`: a second real use case a profile provably
      cannot express.

## Review — Stage C (in progress)

Three bundles migrated, `make lint type test` green at **977 passed**. Two defects found by the
existing suite while doing it, both real and both fixed at the root:

1. **Swallowing `CancelledError` in the connector transport broke the front door's turn timeout.**
   A hung turn ran to completion holding its admission permit — the exact failure
   `service_turn_timeout_seconds` exists to prevent. MAF swallows it in its own MCP paths on the
   grounds that an internal cancel scope is indistinguishable from a real one; at this layer it *is*
   distinguishable (`Task.cancelling()`), and the distinction is load-bearing. Caught by
   `test_stalled_turn_times_out_and_frees_the_permit`, which is why that test is worth its weight.
2. **`AgentProfile.tool_names` could no longer reach a migrated tool.** With the domain capabilities
   behind connectors, a dial that only narrowed the in-process half could not express "a
   property-lookup agent" at all. `tool_names` now spans both halves — narrowing in-process tools
   *and* each connector's allow-list, dropping connectors left with nothing — with one unknown-name
   check over the union, since only that has enough information to tell a typo from a name on the
   other side of the boundary.

Also: connector expectations in tests now derive from `discovered()` rather than hardcoded names, so
adding a bundle does not break unrelated tests.

## Review — Stages A and B

`make lint type test` green: **969 passed, 44 skipped** (the skips are the pre-existing offline set —
26 Postgres, the Temporal-server tests; `temporal.download` and GitHub releases are both blocked by
this sandbox's proxy, so the end-to-end durable test could not be executed here and is CI-gated like
its siblings). `make connector-validate`, `make skill-validate` and `make prose-validate` all pass.

Two things were found by measurement rather than reading, and both changed the design (D-092):

1. **MAF's `header_provider` silently delivers nothing over streamable HTTP.** It is invoked with the
   right values; the server receives no headers, because MAF's ContextVar is set in the calling task
   while the request is issued by the MCP transport's writer task. A request hook on our own httpx
   client works. The transport test now asserts the headers *arrive*, which is the only assertion that
   could have caught this — a unit test of the provider passes either way.
2. **Connectors cannot be process-lived.** Two concurrent turns sharing one connector tool object
   **deadlock**, and any request that did get through would carry the other turn's identity. This is a
   pre-existing hazard on the stdio path (`run_turn` has always entered process-lived tools per turn),
   surfaced by moving capability to HTTP. Fixed at the root: `connector_tools()` builds per turn and
   the caller passes them to `Agent.run(tools=…)`. `build_agent` no longer attaches connectors, so
   profile narrowing of connectors moved to where the set is built.

Cost of that second fix, stated: the front door now owns a `connector_factory` (symmetric with
`agent_factory`, and where per-profile selection attaches in Stage D), and ~18 fake agents in the
suite grew `**_run_options` so a fake cannot silently drift from the real call shape again.
