# D-051 — Foundation review (F4–F7): adversarial review + fixes

Four parallel adversarial reviewers audited F4 (identity/security), F5 (HPC), F6 (deploy), and F7
(seam) over the session's changes. The core paths were confirmed correct (reject-if-absent ordering,
token cache math, OBO non-deputy, TLS None-handling, contextvar reset, audience/alg pinning, cache-key
byte-identity, F7 default behavior preservation). The following real findings were **fixed**:

**F5 (HIGH + hardening).**
- The poll activity's `start_to_close_timeout` was `hpc_mock_run_seconds + qm_activity_timeout` (≈36s)
  — a mock-derived cap that would kill *every* real Nextflow run. `qm_job.py` now branches on
  `hpc_launch_interface`: the nextflow path uses `hpc_run_timeout_seconds` (24h) +
  `hpc_run_heartbeat_timeout_seconds` (120s). New configs added.
- Launcher/artifact HTTP now uses a dedicated `hpc_http_timeout_seconds` (not the Entra-token knob).
- Tower `UNKNOWN` is treated as non-terminal (keep polling), not a hard `FAILED`.

**F4 (misconfig + defense-in-depth).**
- Startup validator: under `entra_required`, `entra_audience` and a tenant/issuer are mandatory (an
  empty audience is a deny-all outage), and `entra_expensive_actions`/`entra_privileged_roles` must be
  set together (declaring one without the other leaves the role gate silently open). A second
  validator rejects a Temporal client cert without its key (half-mTLS).
- `service/app.py` now binds a session to its creator's Entra `oid`; a non-owner gets 404 on
  post/stream (no existence leak) — defense-in-depth beyond the unguessable uuid4.
- `service/auth.py` caches the `PyJWKClient` per endpoint (was rebuilt per request → JWKS re-fetch on
  the hot path) and requires the `exp` claim.

**F6 (CRITICAL + HIGH + medium).**
- `deploy/Containerfile` was missing `kg`, `memory`, `sources` (imported by the entrypoints) → pods
  and the CI smoke import would `ModuleNotFoundError`. Added, and cross-checked against every
  first-party import in the runtime packages.
- NetworkPolicy egress omitted the internal LLM (8000) and OTLP collector (4317) ports → the agent
  could not reach its model / ship traces. Added.
- The MCP Deployment lacked `chemclaw.env`, so its pods had no `CHEMCLAW_POSTGRES_DSN` (fell back to
  localhost). Added.
- The pre-install migrate hook ran before the ConfigMap/ServiceAccount it needs; those are now
  earlier-weighted (-10) pre-install hooks. `deploy.yml` smoke now imports the correct module per
  component (MCP entrypoints were never checked), and the rollout uses `helm upgrade` (runs the hook)
  instead of a nonexistent path.

Accepted deferrals (single-ingest-source cursor, token-exchange lock, ELN-shaped ingest half) are
recorded in `DEFERRED.md`. Tests added: session ownership (`test_service.py`), the enforcement/mTLS
validators (`test_config.py`), and `UNKNOWN`-non-terminal (`test_nextflow_adapter.py`).
