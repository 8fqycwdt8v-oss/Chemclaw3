"""The connector seam: one standardized way to add a capability to Chemclaw.

A connector is a folder — `connectors/<name>/connector.yaml` plus whatever it needs — declaring
everything that capability contributes: the MCP tools its own server serves, the durable jobs its
own
Temporal worker runs, the skills that teach them, and the agent profiles they enable. It is
discovered by folder (as skills are), validated by a pydantic manifest (as `SKILL.md` frontmatter
is), enabled by one config token (as data sources are), and checked by `make connector-validate` in
CI (as the knowledge graph and the skills are). Adding a tool, a durable job, a skill or an agentic
workflow is a bundle and a config token — never an edit to orchestration code.

Layout:

- `manifest.py` — the validated contract (`ConnectorManifest`, the transport and auth unions,
`JobSpec`).
- `registry.py` — discovery, enablement, and building the MCP tools + generated job tools.
- `identity.py` — what travels with a call: the turn's identity as headers, our credential as auth.
- `jobs.py` — one generated durable-launch tool per declared job.
- `health.py` — the startup reachability probe behind `/readyz` and the unhealthy gauge.

The durable half lives in `workflows/connector_job.py`: core's `ConnectorJobWorkflow` keeps
idempotency, actor attribution, the PR-gate and session push-back, while the connector owns the
workflow it wraps. Design and staging: `docs/connector-plan.md`.
"""
