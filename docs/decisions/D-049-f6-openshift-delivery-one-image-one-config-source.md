# D-049 — F6: OpenShift delivery — one image, one config source, three plain secrets (D-A6, D-A6a)

**Context.** The stack must run in-cluster with OIDC, secrets, workers, and probes, without a second
config system and without long-lived client secrets.

**Decision.**
- **One multi-target image** (`deploy/Containerfile`, UBI9, rootless UID 1001, arbitrary-UID safe):
  service, both Temporal workers, and the MCP servers all ship the same bits; `deploy/entrypoint.sh`
  dispatches on `CHEMCLAW_COMPONENT`. No secret baked in.
- **One config source.** The Helm `values.yaml` `config:` block → a `ConfigMap` → `CHEMCLAW_*` env,
  keys mirroring `Settings` exactly. `otel_endpoint` was added and bridged to the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT` in `chemclaw/logging.py` so the collector is one value like the rest.
- **Three plain secrets only** (F6-T6): the generic LLM key (the one Entra exception), Temporal mTLS,
  the HPC-bridge credential. Everything else is Workload Identity Federation (D-045) — the SA is
  annotated, no client secret at rest.
- **D-A6a — Temporal self-hosted in-cluster**, not Temporal Cloud: keeps the durable core inside the
  same OIDC trust boundary and avoids egressing workflow payloads (which carry the Entra `oid`,
  D-044) to a third party. Cloud remains a values-swap (`temporal_api_key` vs the mTLS trio).
- **Migrations as a pre-deploy Helm hook** (`python -m calc.migrate`, D-034) that completes before any
  app container starts. **NetworkPolicy** default-deny egress + allow-list (DNS/Postgres/Temporal/
  HTTPS). Probes: `/readyz`+`/healthz` for the service; the Temporal poll is the workers' liveness.
- **CI** (`deploy.yml`): build + non-root entrypoint smoke, `helm lint`, `helm template | kubeconform`;
  guarded rollout on the default branch.

**Consequence.** The full stack is described as deployable manifests with no second config source and
no stored client secrets beyond the three documented. **Verified offline:** YAML parse, template
brace-balance, `Settings` key mapping. `helm template`/`kubeconform`/the image build are CI-gated —
inherent to a deploy phase (no helm/daemon in the sandbox), not a manifest gap.
