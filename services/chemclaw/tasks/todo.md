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

## Stage A — the seam (core)

- [ ] `connectors/manifest.py` — `ConnectorManifest`, `EndpointSpec` (stdio|http), `ConnectorAuth`
      (none|bearer), `JobSpec`, `JobParam`; all `extra="forbid"`
- [ ] `connectors/identity.py` — header provider (reads the ambient ContextVars per call) +
      `httpx.Auth` per auth mode
- [ ] `connectors/jobs.py` — generated durable tool factory (params model via `create_model`,
      docstring from the manifest, registered through the existing `register_tool`)
- [ ] `workflows/connector_job.py` — `ConnectorJobWorkflow` + `ConnectorJobInput`/`ConnectorJobResult`
- [ ] `connectors/registry.py` — discover → validate → enable → build (MCP tools + job tools)
- [ ] `connectors/health.py` — bounded startup probe
- [ ] `chemclaw/config.py` — `ConnectorSettings`; delete `mcp_servers` + the three MCP spec models
- [ ] `agents/chemclaw_agent.py` — assemble from the connector registry; delete `_mcp_tool`
- [ ] worker registration for `ConnectorJobWorkflow`
- [ ] `/readyz` detail + `chemclaw_connectors_unhealthy` gauge; `connectors_required` fail-fast
- [ ] `scripts/validate_connectors.py` + `make connector-validate`; retarget `validate_skills` and
      `validate_prose_contract` off `settings.mcp_servers`
- [ ] tests: manifest validation, registry enable/unknown, header provider, auth, generated job tool
      (audit+authz wrap it), `ConnectorJobWorkflow` against a real `WorkflowEnvironment`
- [ ] `.env.example`, Helm values/templates, `docs/runbook.md`, `DECISIONS.md` ADRs

Gate A: `make lint type test` green; audit+authz demonstrably wrap a connector-sourced tool and a
generated job tool; unknown enabled connector fails loud; non-loopback `auth: none` refused.

## Stage B — reference bundles + durable-job migration

- [ ] `connectors/molfp/`, `connectors/rxnfp/`: manifest + FastAPI app mounting
      `FastMCP.streamable_http_app()` at `/mcp` plus `/healthz`
- [ ] `connectors/qm/`, `connectors/report/`, `connectors/bo/` job manifests replacing the four
      bespoke adapters in `agents/qm_tools.py` / `agents/durable_tools.py`
- [ ] workflow-id parity test (no in-flight history orphaned)
- [ ] `scripts/connectors_dev.py` + `make connectors`

Gate B: fingerprint search reached over HTTP with identity headers observed; a generated job
completes, pushes back, and PR-gates its note under a real `WorkflowEnvironment`.

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

## Review

(filled in at the end of each stage)
