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

- [ ] `calc`, `chem`, `safety`, then `kg`
- [ ] `bo` moves its workflow to its own worker + task queue (proves plan §5.3 decoupling)
- [ ] Helm: per-connector Deployment/Service + NetworkPolicy

## Stage D — agentic workflow configuration

- [ ] profiles loaded from bundles (`AgentProfile` Stage 3)
- [ ] `POST /sessions {profile}` + one cached agent per profile (Stage 2)

## Stage E — step templates

- [ ] Specified only. Trigger recorded in `BACKLOG.md`: a second real use case a profile provably
      cannot express.

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
